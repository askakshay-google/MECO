#!/usr/bin/env python3

import os
import subprocess
import time
import datetime
import shutil
import sys
import re
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import scrolledtext
import threading
import tempfile
import json
import fnmatch

# --- Default Project/Flow Configuration --- #
DEFAULT_SETUP_REPO_AREA = "<abs path of REPO>"
DEFAULT_ECO_WORK_DIR_NAME = "R1_meco"
DEFAULT_CHIP_NAME = "lajolla"
DEFAULT_PROCESS_NODE = "n2p"
DEFAULT_IP_NAME = "hsio"
DEFAULT_BOB_RUN_TIMEOUT_MINUTES = 1500
DEFAULT_BOB_CHECK_INTERVAL_SECONDS = 30
# --- End Default Configuration --- #

# Constant for key nodes to check for VALID status per stage. Wildcards are supported.
KEY_NODES_PER_STAGE = {
    "pdp": "pdp/dummyfill.merge",
    "pex": "pex/pex.starrc",
    "sta": "sta/sta.bb_sta.*",
    "pceco": "pceco/pceco",
    "pteco": "pteco/eco.primetime", # ADDED for leakage flow
    "pnr": "pnr/chipfinish",
    "applyeco": "pnr/chipfinish"
}

# ADDED: DSA script path
DSA_SCRIPT_PATH = "/google/gchips/workspace/redondo-asia/tpe/user/askakshay/MECO/dsa.py"

DEFAULT_BASE_VAR_FILE_NAME = "base_var_file.var" # This file should exist in the script's directory

DEFAULT_ANALYSIS_INPUT = "{max_tran_eco max_cap_eco setup_eco} {{max_tran_eco max_cap_eco} {setup_eco hold_eco}} {setup_eco hold_eco}  {setup_eco hold_eco max_tran_eco}"


MAIN_ITER_STAGES = ["pdp", "pex", "sta", "pceco", "applyeco"]
LAST_ITER_STAGES = ["pdp", "pex", "sta"]
# ADDED: Leakage-specific stages
LEAKAGE_ITER_STAGES = ["pdp", "pex", "sta", "pteco", "applyeco"]


abort_flag = threading.Event()
continue_event = threading.Event()

failed_info = {
    "is_failed_state": False,
    "branch": None,
    "node_pattern": None,
    "specific_node": None,
    "is_timeout_state": False, # ADDED: To handle timeout state
    "stage": None
}

# ADDED: To handle abort and continue
abort_resume_info = {
    "is_aborted_state": False,
    "branch": None,
    "stage": None
}


def get_script_directory():
    """Returns the directory where the current Python script is located."""
    return os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# Core BOB Logic Functions
# =============================================================================

def run_bob_command(command, work_dir=".", status_updater=None, summary_updater=None, timeout_minutes=DEFAULT_BOB_RUN_TIMEOUT_MINUTES, **kwargs):
    if abort_flag.is_set() and not abort_resume_info["is_aborted_state"]:
        if status_updater: status_updater("INFO: Abort requested. Skipping command.")
        return None 

    cmd_str = ' '.join(command) if isinstance(command, list) else command
    if status_updater: status_updater(f"RUNNING: {cmd_str}  (in {work_dir})")

    try:
        timeout_secs = timeout_minutes * 60 if timeout_minutes > 0 else None

        # The 'encoding' parameter tells subprocess.run to handle string/bytes conversion.
        # Input should be a string if encoding is set.
        result = subprocess.run(
            command, cwd=work_dir, check=False, shell=isinstance(command, str),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', errors='ignore', timeout=timeout_secs, **kwargs
        )
        return result

    except subprocess.TimeoutExpired:
        err_msg = f"ERROR: Command timed out after {timeout_minutes} minutes: {cmd_str}"
        if status_updater: status_updater(err_msg)
        return None
    except FileNotFoundError:
        cmd_name = command[0] if isinstance(command, list) else command.split()[0]
        err_msg = f"ERROR: Command '{cmd_name}' not found. Is 'bob' in your PATH?"
        if status_updater: status_updater(err_msg)
        return None
    except Exception as e:
        cmd_name = command[0] if isinstance(command, list) else command.split()[0]
        err_msg = f"ERROR: Unexpected error running {cmd_str}: {e}"
        if status_updater: status_updater(err_msg)
        return None

def wait_for_bob_run(run_area, branch, node_pattern, timeout_minutes, check_interval_seconds, status_updater=None, summary_updater=None, is_single_node_check=False):
    log_prefix = f"WAIT({node_pattern} in {branch})"
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60 if timeout_minutes > 0 else float('inf')

    if not os.path.isdir(run_area):
        msg = f"ERROR: {log_prefix}: ECO Run directory does not exist: {run_area}"
        if status_updater: status_updater(msg)
        return "ERROR", {}, msg

    timestamp = datetime.datetime.now().strftime("%H:%M:%S_%d-%m-%Y")
    safe_node_pattern_for_file = re.sub(r'[\\/*?"<>|:]', '_', node_pattern)
    temp_log_filename = f"bob_info_{branch}_{safe_node_pattern_for_file}_{timestamp}.json"
    log_file_path = os.path.join(tempfile.gettempdir(), temp_log_filename)

    last_statuses_summary = None
    terminal_states = {"VALID", "FAILED", "INVALID", "ERROR", "KILLED"}
    all_final_statuses = {}

    while True:
        if abort_flag.is_set() and not abort_resume_info["is_aborted_state"]:
            if os.path.exists(log_file_path):
                try: os.remove(log_file_path)
                except OSError: pass
            return "ABORTED", {}, "Abort requested."

        if (time.time() - start_time) > timeout_seconds:
            if os.path.exists(log_file_path):
                try: os.remove(log_file_path)
                except OSError: pass
            return "TIMEOUT", all_final_statuses, f"Timeout after {timeout_minutes}m."


        bob_info_cmd = ["bob", "info", "-r", run_area, "--branch", branch, "-o", log_file_path]
        try:
            if os.path.exists(log_file_path): os.remove(log_file_path)
            result = run_bob_command(bob_info_cmd, work_dir=run_area, status_updater=status_updater, timeout_minutes=2)
            if not result:
                if status_updater: status_updater(f"ERROR: {log_prefix}: 'bob info' command failed to execute.")
                return "COMMAND_FAILED", {}, "'bob info' command failed to execute."
            if result.returncode != 0:
                if status_updater: status_updater(f"WARN: {log_prefix}: 'bob info' failed. Retrying... Stderr: {result.stderr.strip()}")
                time.sleep(check_interval_seconds); continue
        except Exception as e:
            if status_updater: status_updater(f"ERROR: 'bob info' command failed: {e}")
            return "COMMAND_FAILED", {}, f"'bob info' command failed: {e}"

        try:
            with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                jobs_data = json.load(f)

            statuses_found = {}
            total_nodes_in_scope = 0
            nodes_in_terminal_state = 0

            for job in jobs_data:
                jn, jst = job.get("jobname"), job.get("status","").upper()
                if not (jn and jst and job.get("prop.branch") == branch and fnmatch.fnmatch(jn, node_pattern)):
                    continue

                total_nodes_in_scope += 1
                statuses_found[jn] = jst
                if jst in terminal_states:
                    nodes_in_terminal_state += 1
                    all_final_statuses[jn] = jst

                if is_single_node_check:
                    if jst == "VALID":
                        if os.path.exists(log_file_path): os.remove(log_file_path)
                        return "VALID", {jn: "VALID"}, "Node is VALID"
                    if jst in terminal_states:
                        if os.path.exists(log_file_path): os.remove(log_file_path)
                        return "FAILED", {jn: jst}, f"Node '{jn}' has status {jst}"

            summary_text = ", ".join(sorted(list(set(statuses_found.values())))) if statuses_found else "Polling..."
            if summary_text != last_statuses_summary:
                update_msg = f"Branch '{branch}': Waiting for {node_pattern}. Status: {summary_text}"
                if status_updater: status_updater(f"INFO: {log_prefix}: Statuses: {summary_text}")
                if summary_updater: summary_updater(update_msg)
                last_statuses_summary = summary_text

            if not is_single_node_check and total_nodes_in_scope > 0 and nodes_in_terminal_state == total_nodes_in_scope:
                if status_updater: status_updater(f"INFO: {log_prefix}: All {total_nodes_in_scope} nodes have completed.")
                if os.path.exists(log_file_path): os.remove(log_file_path)
                return "COMPLETED", all_final_statuses, "All nodes finished."

        except Exception as e:
            if status_updater: status_updater(f"WARN: {log_prefix}: Failed to process status file: {e}. Retrying...")

        time.sleep(check_interval_seconds)


def create_final_var_file(eco_work_dir_abs, current_branch, prev_branch, block_specific_var_path,
                           base_var_path, block_name, tool_name, status_updater=None, is_leakage_run=False):
    if status_updater:
        status_updater(f"DEBUG: --- Entering create_final_var_file for branch '{current_branch}' ---")
        status_updater(f"DEBUG: eco_work_dir_abs: {eco_work_dir_abs}")
        status_updater(f"DEBUG: base_var_path: {base_var_path}")
        status_updater(f"DEBUG: block_specific_var_path: {block_specific_var_path}")

    op, t, t_name,t_name1, rec = ("ndm", "icc2", "fc", "icc", "bbrecipe_apply setup_fc") if tool_name == "FC" else (("enc.dat", "invs", "innovus", "innovus" , "#Invs flow") if tool_name == "Innovus" else (None, None, None, None, None))
    if not op:
        if status_updater: status_updater(f"ERROR: Invalid TOOL: {tool_name}")
        return None

    try:
        chipfinish_source_dir = ""
        if current_branch == "main":
            found_chipfinish = False
            if block_specific_var_path and os.path.isfile(block_specific_var_path):
                with open(block_specific_var_path, 'r', encoding='utf-8', errors='ignore') as f_block_specific:
                    for line in f_block_specific:
                        m = re.search(r'^\s*bbset\s+chipfinish\.source\s+\{?([^}\s]+)\}?', line, re.IGNORECASE)
                        if m:
                            base_chipfinish_src = m.group(1).strip()
                            if not os.path.isabs(base_chipfinish_src):
                                run_dir_parent = os.path.dirname(os.path.dirname(eco_work_dir_abs))
                                base_chipfinish_src = os.path.abspath(os.path.join(run_dir_parent, base_chipfinish_src))
                            chipfinish_source_dir = base_chipfinish_src
                            found_chipfinish = True
                            if status_updater: status_updater(f"DEBUG: Found chipfinish.source in block specific var: {base_chipfinish_src}")
                            break
            if not found_chipfinish:
                 raise ValueError(f"CRITICAL: 'bbset chipfinish.source' not found in block specific var file: {block_specific_var_path}")
        else:
            if not prev_branch: raise ValueError("CRITICAL: Previous branch name is required for non-main iterations.")
            chipfinish_source_dir = os.path.join(eco_work_dir_abs, prev_branch)

        if status_updater: status_updater(f"DEBUG: Resolved chipfinish_source_dir to: {chipfinish_source_dir}")

        chipfp_dir = os.path.join(chipfinish_source_dir, "pnr", "chipfinish")
        pre_dir_context = os.path.join(eco_work_dir_abs, current_branch)

        # --- MODIFIED: Conditional bbset for applyeco ---
        if is_leakage_run:
            # For leakage, we use the pteco output
            #eco_change_file_path = os.path.join(pre_dir_context, 'pteco', 'eco.primetime', 'changelist_pteco', f'eco_output.post_Hold_revert_final.{t_name1}.tcl')
            eco_change_file_path = os.path.join(pre_dir_context, 'pteco', 'eco.primetime', 'changelist_pteco', f'eco_output.{t_name1}.tcl')
            apply_eco_bbset = f"bbset pnr.applyeco.ECOChangeFile {{{eco_change_file_path}}}"
        else:
            # For regular runs, use the pceco output
            apply_eco_bbset = f"bbset pnr.applyeco.ECOChangeFile {{{os.path.join(pre_dir_context, 'pceco/pceco/outs', f'eco.{t}.tcl')}}}"
        # --- END MODIFICATION ---

        bbsets = f"""\n# --- Auto-gen settings by ECO script for branch: {current_branch} ---
bbset hierarchy.{block_name}.chipfinish.source {{{chipfinish_source_dir}}}
bbset pnr.Tool {t_name}
bbset Tool(pnr) {t_name}
{rec}
bbset pex.FillOasisFiles {{{os.path.join(pre_dir_context, 'pdp/dummyfill/outs', f'{block_name}.dummyfill.beol.oas')}}}
bbset pex.source {{{os.path.join(pre_dir_context, 'pex')}}}
bbset pex.TopDefFile {{{os.path.join(chipfp_dir, 'outs', f'{block_name}.pex.def.gz')}}}
bbset pnr.applyeco.InputDatabase {{{os.path.join(chipfp_dir, 'outs', f'{block_name}.{op}')}}}
{apply_eco_bbset}
bbset pteco.STA_RUN_DIR {{{os.path.join(pre_dir_context, 'sta')}}}
bbset pteco.TOP_CELL_NAME {{ {block_name} }}
bbset pteco.ECO_DESIGN_LIST {{ {block_name} }}
# --- End Auto-gen settings ---\n"""

    except Exception as e:
        if status_updater: status_updater(f"CRITICAL_ERROR: Failed during bbset path generation: {e}")
        return None

    final_var_file_name = f"final_var_file_{current_branch}.var"
    final_var_file_path = os.path.join(eco_work_dir_abs, final_var_file_name)

    try:
        combined_content = ""
        if os.path.isfile(base_var_path):
            with open(base_var_path, 'r', encoding='utf-8', errors='ignore') as f:
                combined_content += f.read() + "\n"
            if status_updater: status_updater(f"DEBUG: Successfully read content from base_var_path: {base_var_path}")
        else:
            if status_updater: status_updater(f"WARN: base_var_path not found or not a file: {base_var_path}")

        if block_specific_var_path and os.path.isfile(block_specific_var_path):
            with open(block_specific_var_path, 'r', encoding='utf-8', errors='ignore') as f:
                combined_content += f.read() + "\n"
            if status_updater: status_updater(f"DEBUG: Successfully read content from block_specific_var_path: {block_specific_var_path}")
        elif block_specific_var_path:
             if status_updater: status_updater(f"WARN: block_specific_var_path was provided but not found: {block_specific_var_path}")

        combined_content += bbsets

        if status_updater: status_updater(f"DEBUG: Writing combined content to: {final_var_file_path}")
        with open(final_var_file_path, 'w', encoding='utf-8', errors='ignore') as f_final:
            f_final.write(combined_content)

        if status_updater: status_updater(f"DEBUG: --- Successfully finished create_final_var_file for branch '{current_branch}' ---")
        return final_var_file_path
    except Exception as e:
        if status_updater: status_updater(f"CRITICAL_ERROR: Failed during final var file creation: {e}")
        return None

def _add_analysis_bbsets_to_var_file(final_var_file_path, current_iter_name,
                                     analysis_sequences_name, status_updater=None):
    specific_iteration_bbsets = ""
    analysis_index = 0 if current_iter_name == "main" else int(current_iter_name.split('_')[1])
    
    if current_iter_name != "Last_iter":
        try:
            if 0 <= analysis_index < len(analysis_sequences_name):
                current_analysis_sequence = analysis_sequences_name[analysis_index]
                
                is_leakage_run = False
                if isinstance(current_analysis_sequence, str):
                    is_leakage_run = (current_analysis_sequence.strip('{}').strip() == "leakage")

                if not is_leakage_run:
                    if isinstance(current_analysis_sequence, list):
                        eco_order_parts = []
                        bbset_lines = []
                        for i, seq in enumerate(current_analysis_sequence, 1):
                            smsa_name = f"SMSA{i}"
                            eco_order_parts.append(smsa_name)
                            bbset_lines.append(f"bbset pceco.{smsa_name} {{{seq.strip()}}}")
                        
                        specific_iteration_bbsets = f"\nbbset pceco.EcoOrder {{{' '.join(eco_order_parts)}}}\n" + "\n".join(bbset_lines) + "\n"
                    else: # It's a string
                        specific_iteration_bbsets = f"\nbbset pceco.EcoOrder {{SMSA1}}\nbbset pceco.SMSA1 {current_analysis_sequence}\n"
        except (ValueError, IndexError):
            if status_updater: status_updater(f"WARN: Could not determine analysis sequence for iteration '{current_iter_name}'.")
            pass

    if specific_iteration_bbsets:
        try:
            with open(final_var_file_path, 'a', encoding='utf-8', errors='ignore') as f_final_var:
                f_final_var.write(specific_iteration_bbsets)
            return True
        except Exception as e:
            if status_updater: status_updater(f"ERROR: Failed to append analysis bbsets to {final_var_file_path}: {e}")
            return False
    return True

def run_eco_logic(base_var_file_path, block_name, tool_name, analysis_sequences_name, num_iterations, eco_work_dir,
                  timeout_minutes, check_interval, dsa_enabled,
                  status_updater, summary_updater, completion_callback, get_gui_block_specific_var_file_func):
    global failed_info, abort_resume_info
    current_stage_for_error, current_branch_for_error, process_outcome = "setup", "N/A", "UNKNOWN"

    try:
        summary_updater("Starting ECO process...")
        status_updater(f"INFO: Starting ECO for BLOCK {block_name}, TOOL {tool_name}")

        all_iterations = ["main"] + [f"Iter_{i}" for i in range(1, num_iterations)] + (["Last_iter"] if num_iterations >=1 else [])
        prev_iter = ""
        block_specific_var_path_for_run = get_gui_block_specific_var_file_func()

        iter_idx = 0
        while iter_idx < len(all_iterations):
            current_iter_name = all_iterations[iter_idx]
            current_branch_for_error = current_iter_name

            # --- MODIFIED: Dynamic stage selection ---
            analysis_index = 0 if current_iter_name == "main" else int(current_iter_name.split('_')[1])
            current_analysis_sequence = analysis_sequences_name[analysis_index]
            is_leakage_run = (isinstance(current_analysis_sequence, str) and current_analysis_sequence.strip('{}').strip() == "leakage")

            if is_leakage_run:
                stages_to_run = LEAKAGE_ITER_STAGES
                status_updater(f"INFO: Leakage iteration detected for '{current_iter_name}'. Using stages: {stages_to_run}")
            else:
                stages_to_run = MAIN_ITER_STAGES if current_iter_name != "Last_iter" else LAST_ITER_STAGES
            # --- END MODIFICATION ---

            start_stage_idx = 0
            if abort_resume_info["is_aborted_state"] and abort_resume_info["branch"] == current_iter_name:
                try:
                    start_stage_idx = stages_to_run.index(abort_resume_info["stage"])
                    status_updater(f"INFO: Resuming from aborted state at Branch: {current_iter_name}, Stage: {abort_resume_info['stage']}")
                    summary_updater(f"Branch '{current_iter_name}': Resuming at stage '{abort_resume_info['stage']}'")
                    status_updater("INFO: Running 'bob start' before continuing...")
                    run_bob_command(["bob", "start", "-r", eco_work_dir], work_dir=eco_work_dir, status_updater=status_updater)
                except ValueError:
                    status_updater(f"WARN: Aborted stage '{abort_resume_info['stage']}' not found in current iteration. Starting from beginning.")
                abort_resume_info["is_aborted_state"] = False
            else:
                summary_updater(f"Branch '{current_iter_name}': Preparing...")
                status_updater(f"\n{'='*20} Prep Branch: '{current_iter_name}' {'='*20}")
                if abort_flag.is_set(): raise RuntimeError("Aborted")

                #B004, reported by Sunil on last iteration split on integar
                current_stage_for_error = f"prepare_var_{current_iter_name}"
                # MODIFIED: Pass is_leakage_run flag
                final_var_file = create_final_var_file(eco_work_dir, current_iter_name, prev_iter, block_specific_var_path_for_run, base_var_file_path, block_name, tool_name, status_updater, is_leakage_run=is_leakage_run)
                #final_var_file = create_final_var_file(eco_work_dir, current_iter_name, prev_iter, block_specific_var_path_for_run, base_var_file_path, block_name, tool_name, status_updater)
                if not final_var_file:
                    raise RuntimeError(f"FAIL: Prepare var file for '{current_iter_name}' See log for details (e.g., missing 'chipfinish.source' in block specific var file).")
                if current_iter_name != "Last_iter":
                    if not _add_analysis_bbsets_to_var_file(final_var_file, current_iter_name, analysis_sequences_name, status_updater):
                        raise RuntimeError(f"FAIL: Could not add analysis bbsets for '{current_iter_name}'.")

                current_stage_for_error = f"create_{current_iter_name}"
                create_cmd = ["bob", "create", "-r", eco_work_dir, "-v", final_var_file, "-s"] + stages_to_run
                if current_iter_name != "main": create_cmd[2:2] = ["--branch", current_iter_name]
                create_res = run_bob_command(create_cmd, work_dir=eco_work_dir, status_updater=status_updater)
                if not create_res or create_res.returncode != 0:
                    raise RuntimeError(f"FAIL: 'bob create' for '{current_iter_name}'.")

            stage_idx = start_stage_idx
            while stage_idx < len(stages_to_run):
                stage_type = stages_to_run[stage_idx]
                
                abort_resume_info["branch"] = current_iter_name
                abort_resume_info["stage"] = stage_type

                if stage_type == "applyeco" : 
                    stage_type = "pnr"
                
                run_node_pattern = f"{stage_type}/*"
                current_stage_for_error = f"run_{current_iter_name}_{stage_type}"
                if abort_flag.is_set(): raise RuntimeError("Aborted")

                key_pattern = KEY_NODES_PER_STAGE.get(stage_type)
                is_dsa_stage = (stage_type == 'sta' and dsa_enabled)
                if is_dsa_stage:
                    key_pattern = "sta/sta.bb_summary"
                    status_updater("INFO: DSA mode enabled. Key node for 'sta' set to 'sta.bb_summary'")
                
                summary_updater(f"Branch '{current_iter_name}': Running stage '{stage_type}'")
                run_cmd = ["bob", "run", "-r", eco_work_dir, "--branch", current_iter_name, "--node", run_node_pattern]
                run_res = run_bob_command(run_cmd, work_dir=eco_work_dir, status_updater=status_updater)
                if not run_res or run_res.returncode != 0:
                    raise RuntimeError(f"'bob run' command failed for {run_node_pattern}")

                summary_updater(f"Branch '{current_iter_name}': Waiting for stage '{stage_type}'")
                wait_status, all_statuses, wait_msg = wait_for_bob_run(eco_work_dir, current_iter_name, key_pattern, timeout_minutes, check_interval, status_updater, summary_updater)
                
                if wait_status == "COMMAND_FAILED":
                    failed_info.update({"is_failed_state": True, "branch": current_iter_name, "node_pattern": "bob info", "specific_node": "bob info"})
                    summary_updater(f"Branch '{current_iter_name}': FAILED at 'bob info'. User action needed.")
                    process_outcome = "FAILED"
                    status_updater("ERROR: 'bob info' command failed. User action required.")
                    completion_callback(process_outcome)
                    continue_event.wait()
                    continue_event.clear()
                    if abort_flag.is_set():
                        raise RuntimeError("Aborted")
                    failed_info["is_failed_state"] = False
                    summary_updater(f"Branch '{current_iter_name}': Continue pressed, retrying stage: {stage_type}")
                    continue

                if wait_status == "TIMEOUT":
                    failed_info.update({"is_timeout_state": True, "branch": current_iter_name, "stage": stage_type})
                    summary_updater(f"Branch '{current_iter_name}': TIMEOUT at stage '{stage_type}'. User action needed.")
                    process_outcome = "TIMEOUT"
                    
                    run_bob_command(["bob", "stop", "-r", eco_work_dir], work_dir=eco_work_dir, status_updater=status_updater)
                    
                    completion_callback(process_outcome) 
                    
                    continue_event.wait()
                    continue_event.clear()
                    
                    if abort_flag.is_set():
                        raise RuntimeError("Aborted")

                    failed_info["is_timeout_state"] = False
                    summary_updater(f"Branch '{current_iter_name}': Continue pressed, restarting stage: {stage_type}")
                    
                    status_updater(f"INFO: Running 'bob start' after timeout...")
                    run_bob_command(["bob", "start", "-r", eco_work_dir], work_dir=eco_work_dir, status_updater=status_updater)

                    status_updater(f"INFO: Re-running stage '{stage_type}' after timeout...")
                    run_bob_command(run_cmd, work_dir=eco_work_dir, status_updater=status_updater) 
                    
                    continue 

                
                if wait_status != "COMPLETED":
                    if wait_status == "ABORTED":
                         raise RuntimeError("Aborted")
                    raise RuntimeError(f"{wait_status} waiting for {stage_type}: {wait_msg}")

                failed_key_node = next((name for name, status in all_statuses.items() if status != "VALID" and fnmatch.fnmatch(name, key_pattern or "")), None)

                if failed_key_node:
                    while True: # Key Node Failure Loop
                        failed_info.update({"is_failed_state": True, "branch": current_iter_name, "node_pattern": key_pattern, "specific_node": failed_key_node})
                        summary_updater(f"Branch '{current_iter_name}': FAILED at '{failed_key_node}'. User action needed.")
                        process_outcome="FAILED"
                        status_updater(f"ERROR: Key node '{failed_key_node}' failed. User action required.")
                        send_email("MECO Failed", f"MECO failed at branch {current_iter_name} stage {stage_type} node {failed_key_node}. Please update the block specific var file if required , save under a different filename, and reupload the same and press continue. Current Status: FAILED")
                        completion_callback(process_outcome)
                        continue_event.wait(); continue_event.clear()
                        if abort_flag.is_set(): raise RuntimeError("Aborted")

                        new_block_specific_path = get_gui_block_specific_var_file_func()
                        if new_block_specific_path != block_specific_var_path_for_run:
                            status_updater("INFO: New block-specific var file detected. Re-creating final var file...")
                            summary_updater(f"Branch '{current_iter_name}': Updating var file...")
                            block_specific_var_path_for_run = new_block_specific_path
                            new_var_file = create_final_var_file(eco_work_dir, current_iter_name, prev_iter, block_specific_var_path_for_run, base_var_file_path, block_name, tool_name, status_updater, is_leakage_run=is_leakage_run)
                            if not new_var_file or not _add_analysis_bbsets_to_var_file(new_var_file, current_iter_name, analysis_sequences_name, status_updater):
                                status_updater("ERROR: Failed to recreate var file. Please try again."); summary_updater("Error creating var file."); continue



             

                            status_updater("INFO: Updating flow for pending nodes...")
                            
                            # Stages to be updated using the 'bob update' command
                            bob_update_stages = {"sta", "pceco", "pteco"}
                            
                            # First, handle the special stages with 'bob update'
                            for stage_to_update in bob_update_stages:
                                # Check if this stage is actually in the list of remaining stages to be run
                                if stage_to_update in stages_to_run[stage_idx:]:
                                    status_updater(f"INFO: Updating flow for stage '{stage_to_update}' using 'bob update'.")
                                    
                                    # Command to delete the old stage definition
                                    bob_update_force_cmd = ["bob", "update", "flow", "-r", eco_work_dir, "-d", stage_to_update, "--force", "--branch", current_iter_name]
                                    update_force_res = run_bob_command(bob_update_force_cmd, work_dir=eco_work_dir, status_updater=status_updater)
                                    if not update_force_res or update_force_res.returncode != 0:
                                        status_updater(f"ERROR: 'bob update --force' command failed for stage {stage_to_update}.")
                                        # Continue to the add command anyway, as it might fix the issue
                                        
                                    # Command to add the stage back with the new variables
                                    bob_update_add_cmd = ["bob", "update", "flow", "-r", eco_work_dir, "-a", stage_to_update, "--force", "--branch", current_iter_name]
                                    update_add_res = run_bob_command(bob_update_add_cmd, work_dir=eco_work_dir, status_updater=status_updater)
                                    if not update_add_res or update_add_res.returncode != 0:
                                        status_updater(f"ERROR: 'bob update -a' command failed for stage {stage_to_update}.")
                            
                            # Then, iterate through all pending stages and handle the rest by updating flow.var
                            status_updater("INFO: Updating flow.var for other pending nodes...")
                            for s_idx in range(stage_idx, len(stages_to_run)):
                                pending_stage = stages_to_run[s_idx]
                                
                                # Skip the stages already handled by 'bob update'
                                if pending_stage in bob_update_stages:
                                    continue
                            
                                status_updater(f"INFO: Updating vars for stage '{pending_stage}' by replacing flow.var.")
                                
                                # Correctly handle 'applyeco' by pointing to the 'pnr' directory
                                stage_dir_name = "pnr" if pending_stage == "applyeco" else pending_stage
                                pending_stage_dir = os.path.join(eco_work_dir, current_iter_name, stage_dir_name)
                            
                                if not os.path.isdir(pending_stage_dir):
                                    status_updater(f"WARN: Directory for stage '{pending_stage}' not found at {pending_stage_dir}, cannot update vars.")
                                    continue
                            
                                # Iterate through all subdirectories (e.g., dummyfill, pex.starrc) within the stage directory
                                for subdir in os.listdir(pending_stage_dir):
                                    node_dir_path = os.path.join(pending_stage_dir, subdir)
                                    if not os.path.isdir(node_dir_path): continue
                            
                                    flow_var_path = os.path.join(node_dir_path, "vars", "flow.var")
                                    if os.path.isfile(flow_var_path):
                                        try:
                                            # Create a backup of the old var file
                                            old_var_path = os.path.join(node_dir_path, "vars", "flow.var.old")
                                            shutil.move(flow_var_path, old_var_path)
                                            # Copy the new var file into place
                                            shutil.copy(new_var_file, flow_var_path)
                                            status_updater(f"  - Updated: {flow_var_path}")
                                        except Exception as e:
                                            status_updater(f"ERROR: Failed updating {flow_var_path}: {e}")
                            





                        failed_info["is_failed_state"] = False
                        key_node_for_stage = KEY_NODES_PER_STAGE.get(stage_type)
                        summary_updater(f"Branch '{current_iter_name}': Continue pressed, re-running node: {key_node_for_stage}")
                        
                        rerun_key_node_cmd = ["bob", "run", "-r", eco_work_dir, "--branch", current_iter_name, "--node", key_node_for_stage, "-f"]
                        
                        rerun_res = run_bob_command(rerun_key_node_cmd, work_dir=eco_work_dir, status_updater=status_updater)
                        if not rerun_res or rerun_res.returncode != 0:
                            status_updater(f"ERROR: Failed to rerun '{key_node_for_stage}'. Try again."); continue

                        retry_status, _, _ = wait_for_bob_run(eco_work_dir, current_iter_name, key_node_for_stage, timeout_minutes, check_interval, status_updater, summary_updater, is_single_node_check=True)
                        if retry_status == "VALID":
                            status_updater(f"INFO: Retry SUCCESS for '{key_node_for_stage}'. Resuming stage check.")
                            summary_updater(f"Branch '{current_iter_name}': SUCCESS, node '{key_node_for_stage}' is VALID. Continuing...")
                            break
                        else:
                             status_updater(f"ERROR: Retry for '{key_node_for_stage}' also failed. Try again.")
                             summary_updater(f"Branch '{current_iter_name}': FAILED, node '{key_node_for_stage}' failed again.")
                    continue
                else: # Stage passed
                    if is_dsa_stage:
                        summary_updater(f"Branch '{current_iter_name}': sta stage valid, running DSA flow...")
                        status_updater("INFO: 'sta/sta.bb_summary' is VALID. Running DSA post-processing.")
                        
                        iter_area = os.path.join(eco_work_dir, current_iter_name)
                        merge_dir = os.path.join(iter_area, "sta", "merge")
                        dsa_output_file = os.path.join(merge_dir, "dsa_ufs")
                        scenarios_file = os.path.join(iter_area, "sta", "scenarios.txt")

                        dsa_cmd = f"python3 {DSA_SCRIPT_PATH} --merge_dir {merge_dir} --output {dsa_output_file}"
                        dsa_res = run_bob_command(dsa_cmd, work_dir=iter_area, status_updater=status_updater, summary_updater=summary_updater)
                        if not dsa_res or dsa_res.returncode != 0:
                            raise RuntimeError("DSA script execution failed.")
                        
                        #grep_cmd = f"grep '^.*[a-z]' {dsa_output_file}/* | grep % | grep -v scenario | awk -F '[|%]' '$3 > 70' | column -t | awk '{{print $2}}' | sort -uk 1 > {scenarios_file}"
                        grep_cmd = f"grep '^.*[a-z]' {dsa_output_file}/* | grep % | grep -v scenario | sed 's/%//g' | awk '$8 < 90'  | column -t | awk '{{print $2}}' | sort -uk 1 > {scenarios_file}"
                        status_updater(f"INFO: Running scenario generation command: {grep_cmd}")
                        try:
                           subprocess.run(grep_cmd, shell=True, check=True, cwd=iter_area, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                           status_updater(f"INFO: Successfully created scenarios.txt at {scenarios_file}")
                        except subprocess.CalledProcessError as e:
                           raise RuntimeError(f"Scenario generation command failed: {e.stderr.decode('utf-8')}")
                        
                        setup_scenarios = []
                        hold_scenarios = []
                        if os.path.exists(scenarios_file):
                            with open(scenarios_file, 'r') as f:
                                for line in f:
                                    scenario = line.strip()
                                    if scenario.endswith('_t'):
                                        setup_scenarios.append(scenario)
                                    else:
                                        hold_scenarios.append(scenario)

                        bbset_lines = "\n# --- DSA Scenarios ---\n"
                        if setup_scenarios:
                            bbset_lines += f"bbset pceco.SetupScenarios {{{' '.join(setup_scenarios)}}}\n"
                        if hold_scenarios:
                             bbset_lines += f"bbset pceco.HoldScenarios {{{' '.join(hold_scenarios)}}}\n"

                        final_var_file_path = os.path.join(eco_work_dir, f"final_var_file_{current_iter_name}.var")
                        flow_var_path = os.path.join(iter_area, "vars", "flow.var")
                        
                        try:
                            with open(final_var_file_path, 'a') as f: f.write(bbset_lines)
                            status_updater(f"INFO: Appended DSA scenarios to {final_var_file_path}")
                            if os.path.exists(flow_var_path):
                                with open(flow_var_path, 'a') as f: f.write(bbset_lines)
                                status_updater(f"INFO: Appended DSA scenarios to {flow_var_path}")
                            else:
                                status_updater(f"WARN: flow.var not found at {flow_var_path}, skipping append.")
                        except Exception as e:
                            raise RuntimeError(f"Failed to append DSA scenarios to var files: {e}")

                        # MODIFIED: Only run bob update if not a leakage run
                        if not is_leakage_run:
                            bob_update_force_cmd = ["bob", "update", "flow", "-r", eco_work_dir, "-d", "pceco", "--force", "--branch", current_iter_name]
                            bob_update_add_cmd = ["bob", "update", "flow", "-r", eco_work_dir, "-a", "pceco", "--force", "--branch", current_iter_name]
                            
                            update_force_res = run_bob_command(bob_update_force_cmd, work_dir=eco_work_dir, status_updater=status_updater, summary_updater=summary_updater)
                            if not update_force_res or update_force_res.returncode != 0:
                                raise RuntimeError("bob update --force command failed.")
                            
                            update_add_res = run_bob_command(bob_update_add_cmd, work_dir=eco_work_dir, status_updater=status_updater, summary_updater=summary_updater)
                            if not update_add_res or update_add_res.returncode != 0:
                                raise RuntimeError("bob update -a command failed.")

                    for name, status in all_statuses.items():
                        if status != "VALID" and not (key_pattern and fnmatch.fnmatch(name, key_pattern)):
                            status_updater(f"WARN: Non-key node '{name}' has status '{status}'. Continuing.")
                    stage_idx += 1
            
            prev_iter = current_iter_name
            iter_idx += 1

        process_outcome = "SUCCESS"
        summary_updater("All iterations completed successfully.")
        status_updater(f"\n{'='*20} All iterations COMPLETED successfully. {'='*20}")
    except Exception as e:
        process_outcome = "ERROR" if not abort_flag.is_set() else "ABORTED"
        summary_updater(f"PROCESS {process_outcome}!")
        err_msg = f"\n{'!'*20} ECO PROCESS {process_outcome} {'!'*20}\n"
        if process_outcome == "ABORTED":
            err_msg += f"ABORTED @ Branch: {abort_resume_info.get('branch', 'N/A')}, Stage: {abort_resume_info.get('stage', 'N/A')}\n"
        else:
            err_msg += f"ERROR @ Branch: {current_branch_for_error}, Stage: {current_stage_for_error}\nDETAILS: {e}\n"
        status_updater(err_msg)
    finally:
        if process_outcome == "ABORTED":
            abort_resume_info["is_aborted_state"] = True
            send_email("MECO Failed", f"MECO fails at branch: {abort_resume_info.get('branch', 'N/A')} and stage {abort_resume_info.get('stage', 'N/A')}. Please fix the issue, change the block specific var file if required and click on Continue")
            completion_callback(process_outcome)
        elif not failed_info.get("is_failed_state", False) and not failed_info.get("is_timeout_state", False):
            completion_callback(process_outcome)


def send_email(subject, body):
    """Sends an email using the command line mail tool."""
    try:
        username_bytes = subprocess.check_output(['whoami'])
        username = username_bytes.decode('utf-8').strip()
        recipient = f"{username}@google.com"
        command = f'echo "{body}" | mail -s "{subject}" {recipient}'
        subprocess.run(command, shell=True, check=True)
    except Exception as e:
        print(f"Failed to send email: {e}")

class EcoRunnerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multi-ECO Utility (v1.20)") # Version updated
        self.geometry("850x650")
        self.processing_thread = None
        self.setup_thread = None
        self.is_log_visible = False
        self.log_file_path = None

        self.style = ttk.Style(self)
        try:
            self.style.configure("Start.TButton", foreground="black", background="lightgreen", padding=6, font=('TkDefaultFont', 10, 'bold')); self.style.map("Start.TButton", background=[('active', 'green'), ('disabled', 'grey')])
            self.style.configure("Continue.TButton", foreground="white", background="blue", padding=6, font=('TkDefaultFont', 10, 'bold')); self.style.map("Continue.TButton", background=[('active', 'darkblue'), ('disabled', 'grey')])
            self.style.configure("Abort.TButton", foreground="white", background="red", padding=6, font=('TkDefaultFont', 10, 'bold')); self.style.map("Abort.TButton", background=[('active', '#E50000'), ('disabled', 'grey')])
            self.style.configure("Quit.TButton", foreground="black", background="#FFA07A", padding=6); self.style.map("Quit.TButton", background=[('active', '#FF7F50')])
            self.style.configure("Clear.TButton", foreground="black", background="lightyellow", padding=6); self.style.map("Clear.TButton", background=[('active', 'yellow'), ('disabled', 'grey')])
            self.style.configure("File.TButton", padding=3);
        except tk.TclError:
            print("Warning: ttk Button styling might not be fully supported on this Tkinter version.")

        config_frame = ttk.LabelFrame(self, text="Configuration", padding="15"); config_frame.pack(fill=tk.X, padx=10, pady=5); current_row = 0

        config_io_frame = ttk.Frame(config_frame); config_io_frame.grid(row=current_row, column=0, columnspan=4, pady=(0,10), sticky=tk.W)
        self.load_config_button = ttk.Button(config_io_frame, text="Load Config", command=self.load_configuration, style="File.TButton"); self.load_config_button.pack(side=tk.LEFT, padx=(0,5))
        self.save_config_button = ttk.Button(config_io_frame, text="Save Config", command=self.save_configuration, style="File.TButton"); self.save_config_button.pack(side=tk.LEFT, padx=5); current_row += 1

        ttk.Label(config_frame, text="Repo Area (for setup):", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.repo_area_var = tk.StringVar(value=DEFAULT_SETUP_REPO_AREA)
        self.repo_area_entry = ttk.Entry(config_frame, textvariable=self.repo_area_var, width=100)
        self.repo_area_entry.grid(row=current_row, column=1, columnspan=2, padx=5, pady=3, sticky=tk.EW)
        self.repo_area_browse = ttk.Button(config_frame, text="Browse...", command=lambda: self.browse_directory(self.repo_area_var), style="File.TButton"); self.repo_area_browse.grid(row=current_row, column=3, padx=5, pady=3); current_row+=1

        ttk.Label(config_frame, text="IP Name:", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W); self.ip_var = tk.StringVar(value=DEFAULT_IP_NAME); self.ip_entry = ttk.Entry(config_frame, textvariable=self.ip_var, width=30); self.ip_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W);
        ttk.Label(config_frame, text="Chip Name:", foreground="darkgreen").grid(row=current_row, column=2, padx=(20,5), pady=3, sticky=tk.W); self.chip_var = tk.StringVar(value=DEFAULT_CHIP_NAME); self.chip_entry = ttk.Entry(config_frame, textvariable=self.chip_var, width=30); self.chip_entry.grid(row=current_row, column=3, padx=5, pady=3, sticky=tk.W); current_row+=1
        ttk.Label(config_frame, text="Process:", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W); self.process_var = tk.StringVar(value=DEFAULT_PROCESS_NODE); self.process_entry = ttk.Entry(config_frame, textvariable=self.process_var, width=30); self.process_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W); 

        self.dsa_var = tk.BooleanVar(value=False)
        self.dsa_checkbutton = ttk.Checkbutton(config_frame, text="DSA", variable=self.dsa_var)
        self.dsa_checkbutton.grid(row=current_row, column=2, padx=(20,5), pady=3, sticky=tk.W)
        current_row += 1

        ttk.Label(config_frame, text="ECO Work Dir Name :", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.eco_work_dir_name_var = tk.StringVar(value=DEFAULT_ECO_WORK_DIR_NAME)
        self.eco_work_dir_name_entry = ttk.Entry(config_frame, textvariable=self.eco_work_dir_name_var, width=100)
        self.eco_work_dir_name_entry.grid(row=current_row, column=1, columnspan=2, padx=5, pady=3, sticky=tk.EW); current_row+=1

        ttk.Label(config_frame, text="Idle Timeout (minutes):", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.timeout_var = tk.StringVar(value=str(DEFAULT_BOB_RUN_TIMEOUT_MINUTES))
        self.timeout_entry = ttk.Entry(config_frame, textvariable=self.timeout_var, width=10)
        self.timeout_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W)
        ttk.Label(config_frame, text="Check Interval (seconds):", foreground="darkblue").grid(row=current_row, column=2, padx=(20,5), pady=3, sticky=tk.W)
        self.interval_var = tk.StringVar(value=str(DEFAULT_BOB_CHECK_INTERVAL_SECONDS))
        self.interval_entry = ttk.Entry(config_frame, textvariable=self.interval_var, width=10)
        self.interval_entry.grid(row=current_row, column=3, padx=5, pady=3, sticky=tk.W)
        current_row += 1

        ttk.Label(config_frame, text="BLOCK Name:", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W); self.block_var = tk.StringVar(); self.block_entry = ttk.Entry(config_frame, textvariable=self.block_var, width=30); self.block_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W)
        ttk.Label(config_frame, text="TOOL:", foreground="darkblue").grid(row=current_row, column=2, padx=(20,5), pady=3, sticky=tk.W); self.tool_var = tk.StringVar(); self.tool_combo = ttk.Combobox(config_frame, textvariable=self.tool_var, values=["FC", "Innovus"], state="readonly", width=10); self.tool_combo.grid(row=current_row, column=3, padx=5, pady=3, sticky=tk.W+tk.E); self.tool_combo.set("FC"); current_row+=1

        ttk.Label(config_frame, text="Block Specific Var File (optional):", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W); self.block_specific_var_file_var = tk.StringVar(); self.block_specific_var_file_entry = ttk.Entry(config_frame, textvariable=self.block_specific_var_file_var, width=60); self.block_specific_var_file_entry.grid(row=current_row, column=1, columnspan=2, padx=5, pady=3, sticky=tk.EW); self.block_specific_var_file_browse = ttk.Button(config_frame, text="Browse...", command=lambda: self.browse_file(self.block_specific_var_file_var), style="File.TButton"); self.block_specific_var_file_browse.grid(row=current_row, column=3, padx=5, pady=3); current_row+=1

        ttk.Label(config_frame, text="Analysis per Iteration :", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W); self.analysis_input_text = tk.Text(config_frame, width=50, height=3, wrap=tk.WORD); self.analysis_input_text.grid(row=current_row, column=1, columnspan=3, padx=5, pady=3, sticky=tk.EW); self.analysis_input_text.insert(tk.END, DEFAULT_ANALYSIS_INPUT); current_row+=1
        config_frame.columnconfigure(1, weight=1)

        control_frame = ttk.Frame(self, padding="10"); control_frame.pack(fill=tk.X, pady=5)
        self.start_button = ttk.Button(control_frame, text="START ECO", command=self.start_processing, style="Start.TButton"); self.start_button.pack(side=tk.LEFT, padx=5)
        self.continue_button = ttk.Button(control_frame, text="CONTINUE", command=self.continue_processing, style="Continue.TButton", state=tk.DISABLED); self.continue_button.pack(side=tk.LEFT, padx=5)
        self.abort_button = ttk.Button(control_frame, text="ABORT", command=self.abort_processing, style="Abort.TButton", state=tk.DISABLED); self.abort_button.pack(side=tk.LEFT, padx=5)
        self.clear_button = ttk.Button(control_frame, text="Clear ECO Work Dir", command=self.clear_runs, style="Clear.TButton"); self.clear_button.pack(side=tk.LEFT, padx=5)
        self.quit_button = ttk.Button(control_frame, text="Quit", command=self.quit_app, style="Quit.TButton"); self.quit_button.pack(side=tk.RIGHT, padx=10)

        summary_frame = ttk.Frame(self, padding=(10,5,10,5)); summary_frame.pack(fill=tk.X)
        ttk.Label(summary_frame, text="Status:", font=('TkDefaultFont', 10, 'bold')).pack(side=tk.LEFT)
        self.summary_var = tk.StringVar(value="Idle."); self.summary_label = ttk.Label(summary_frame, textvariable=self.summary_var, font=('TkDefaultFont',10,'italic'), foreground="navy", anchor=tk.W); self.summary_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        log_control_frame = ttk.Frame(self, padding=(10,0,10,5)); log_control_frame.pack(fill=tk.X)
        self.log_toggle_var = tk.BooleanVar(value=False); self.log_toggle_check = ttk.Checkbutton(log_control_frame, text="Show Detailed Log", variable=self.log_toggle_var, command=self.toggle_log_display); self.log_toggle_check.pack(side=tk.LEFT)
        self.status_frame = ttk.LabelFrame(self, text="Status Log", padding="10"); self.status_text = scrolledtext.ScrolledText(self.status_frame, wrap=tk.WORD, height=15, state=tk.DISABLED, font=("Courier New",9)); self.status_text.pack(fill=tk.BOTH, expand=True); self.toggle_log_display()

        footer_frame = ttk.Frame(self, padding=(10, 5)); footer_frame.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(footer_frame, text="Developed by askakshay", foreground="gray").pack(side=tk.RIGHT)

    def update_status_display(self, message):
        log_msg = f"{datetime.datetime.now().strftime('%H:%M:%S %d-%m-%Y')} - {message}\n"
        self.after(0, self._update_log, log_msg)
        if self.log_file_path:
            try:
                with open(self.log_file_path, 'a', encoding='utf-8') as f:
                    f.write(log_msg)
            except Exception as e:
                print(f"Error writing to log file: {e}")

    def _update_log(self, log_msg):
        self.status_text.config(state=tk.NORMAL)
        self.status_text.insert(tk.END, log_msg)
        if self.log_toggle_var.get():
            self.status_text.see(tk.END)
        self.status_text.config(state=tk.DISABLED)

    def toggle_log_display(self):
        if self.log_toggle_var.get():
            if not self.status_frame.winfo_viewable():
                self.status_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0,5)); self.geometry("")
        else:
            if self.status_frame.winfo_viewable():
                self.status_frame.pack_forget(); self.geometry("")

    def browse_file(self, var): var.set(filedialog.askopenfilename() or var.get())
    def browse_directory(self, var): var.set(filedialog.askdirectory() or var.get())

    def set_controls_state(self, mode):
        is_running = (mode == 'running')
        is_failed = (mode == 'failed')
        is_aborted = (mode == 'aborted')
        is_idle = (mode == 'idle')
        is_timeout = (mode == 'timeout')

        state_map = {'idle': tk.NORMAL, 'running': tk.DISABLED, 'failed': tk.DISABLED, 'aborted': tk.DISABLED, 'timeout': tk.DISABLED}
        idle_state = state_map[mode]

        for widget in [self.start_button, self.clear_button, self.load_config_button, self.save_config_button,
                       self.repo_area_entry, self.repo_area_browse, self.ip_entry, self.chip_entry,
                       self.process_entry, self.eco_work_dir_name_entry, self.block_entry, self.timeout_entry,
                       self.interval_entry, self.analysis_input_text, self.dsa_checkbutton]:
            widget.config(state=idle_state)

        self.tool_combo.config(state="readonly" if is_idle else tk.DISABLED)
        self.block_specific_var_file_entry.config(state=tk.NORMAL if is_idle or is_failed or is_timeout else tk.DISABLED)
        self.block_specific_var_file_browse.config(state=tk.NORMAL if is_idle or is_failed or is_timeout else tk.DISABLED)
        
        self.continue_button.config(state=tk.NORMAL if is_failed or is_aborted or is_timeout else tk.DISABLED)
        self.abort_button.config(state=tk.NORMAL if is_running else tk.DISABLED)
        self.start_button.config(state=tk.NORMAL if is_idle else tk.DISABLED)


    def processing_complete(self, outcome):
        self.processing_thread = None
        self.update_status_display(f"PROCESS: Outcome = {outcome}")

        if outcome == "SUCCESS":
            self.summary_var.set("SUCCESS: ECO process completed.")
            messagebox.showinfo("Success", "ECO process finished successfully.")
            self.set_controls_state('idle')
            abort_flag.clear()
            abort_resume_info["is_aborted_state"] = False
        elif outcome == "FAILED":
            self.summary_var.set(f"FAILED: Awaiting user action for node: {failed_info.get('specific_node','N/A')}")
            messagebox.showwarning("Failed", f"Run failed @ key node: {failed_info.get('specific_node','N/A')}\nFix issue (check log), optionally update var file, then click CONTINUE or ABORT.")
            self.set_controls_state('failed')
        elif outcome == "TIMEOUT":
            branch = failed_info.get('branch', 'N/A')
            stage = failed_info.get('stage', 'N/A')
            self.summary_var.set(f"TIMEOUT: Awaiting user action for stage: {stage}")
            send_email("MECO timed out", f"MECO timed out at branch {branch} stage {stage}. Either rerun with an increased limit or check for the issue, update the block specifc var if required and click on continue")
            messagebox.showinfo("Timeout", f"MECO timed out at stage {stage}. Smells like a stale job?")
            self.set_controls_state('timeout')
        elif outcome == "ABORTED":
            last_branch = abort_resume_info.get('branch', 'N/A')
            last_stage = abort_resume_info.get('stage', 'N/A')
            self.summary_var.set(f"Process Aborted. Last Branch: {last_branch}, Last Stage: {last_stage}")
            messagebox.showinfo("Aborted", f"Process was aborted.\nLast known location:\nBranch: {last_branch}\nStage: {last_stage}\n\nPress CONTINUE to resume from this stage or QUIT.")
            self.set_controls_state('aborted')
        else: # ERROR
            self.summary_var.set(f"Process ended with status: {outcome}")
            messagebox.showerror("Error", f"Process ended with status: {outcome}\nCheck log for details.")
            self.set_controls_state('idle')
            abort_flag.clear()
            abort_resume_info["is_aborted_state"] = False

    def continue_processing(self):
        if abort_resume_info.get("is_aborted_state"):
            self.summary_var.set("Attempting to resume from aborted state...")
            self.start_processing(is_resume=True)
        elif failed_info.get("is_failed_state") or failed_info.get("is_timeout_state"):
            self.set_controls_state('running')
            continue_event.set()


    def abort_processing(self):
        self.abort_button.config(state=tk.DISABLED)
        last_branch = abort_resume_info.get('branch', 'N/A')
        last_stage = abort_resume_info.get('stage', 'N/A')
        self.summary_var.set(f"Aborting... Last Stage: {last_stage}")
        
        def stop_bob():
            repo_area = self.repo_area_var.get().strip()
            run_name = self.eco_work_dir_name_var.get().strip()
            if repo_area and run_name:
                 eco_run_dir = os.path.join(os.path.abspath(repo_area), "run", run_name)
                 self.update_status_display("INFO: User abort requested. Sending 'bob stop'...")
                 run_bob_command(["bob", "stop", "-r", eco_run_dir], status_updater=self.update_status_display)
            else:
                 self.update_status_display("WARN: Could not determine run area for 'bob stop'.")
            abort_flag.set()
            continue_event.set()

        threading.Thread(target=stop_bob, daemon=True).start()


    def clear_runs(self):
        repo_area = self.repo_area_var.get().strip()
        eco_work_dir_name = self.eco_work_dir_name_var.get().strip()
        if not repo_area or not eco_work_dir_name:
            messagebox.showerror("Input Error", "Specify Repo Area and ECO Work Dir Name to clear."); return

        eco_work_dir_to_clear_abs = os.path.join(os.path.abspath(repo_area), "run", eco_work_dir_name)
        if not os.path.isdir(eco_work_dir_to_clear_abs):
            messagebox.showinfo("Not Found", f"ECO Work Dir not found, nothing to clear:\n{eco_work_dir_to_clear_abs}"); return

        if messagebox.askyesno("Confirm Clear", f"Delete ALL contents of:\n{eco_work_dir_to_clear_abs}\n\nThis CANNOT be undone!"):
            self.set_controls_state('running')
            self.summary_var.set(f"Clearing directory: {eco_work_dir_name}...")
            try:
                shutil.rmtree(eco_work_dir_to_clear_abs)
                self.update_status_display(f"INFO: Successfully cleared {eco_work_dir_to_clear_abs}")
                self.summary_var.set("Directory cleared successfully.")
            except Exception as e:
                self.update_status_display(f"ERROR: Failed to clear directory: {e}")
                self.summary_var.set("Error clearing directory.")
            finally:
                self.set_controls_state('idle')

    def start_processing(self, is_resume=False):
        """Main entry point. Validates inputs, checks workspace, then starts setup or ECO run."""
        self.set_controls_state('running')
        
        if not is_resume:
            if self.log_toggle_var.get():
                self.status_text.config(state=tk.NORMAL); self.status_text.delete('1.0', tk.END); self.status_text.config(state=tk.DISABLED)
            abort_flag.clear(); continue_event.clear()
            failed_info.update({'is_failed_state': False, 'branch': None, 'node_pattern': None, 'specific_node': None, 'is_timeout_state': False, 'stage': None})
            abort_resume_info.update({'is_aborted_state': False, 'branch': None, 'stage': None})
        else:
             abort_flag.clear(); continue_event.clear()


        try:
            params = self._get_and_validate_params()
        except ValueError as e:
            messagebox.showerror("Input Error", str(e)); self.set_controls_state('idle'); return

        repo_area_abs = os.path.abspath(params["repo_area"])
        workspace_exists = os.path.isdir(os.path.join(repo_area_abs, "run")) and os.path.isdir(os.path.join(repo_area_abs, "repo"))
        
        eco_logic_args_tuple = self._prepare_eco_logic_args(params)

        if workspace_exists:
            self.update_status_display(f"INFO: BOB 'run'/'repo' subdirs exist in {repo_area_abs}. Skipping 'bob wa create'.")
            self.update_summary_display("Repo Area ready. Starting ECO run...")
            
            eco_run_dir_abs = os.path.join(repo_area_abs, "run", params["eco_work_dir_name"])
            self._setup_logging(eco_run_dir_abs, params["eco_work_dir_name"])
            
            try:
                target_cwd_for_eco = os.path.join(repo_area_abs, "run")
                os.chdir(target_cwd_for_eco)
                self.update_status_display(f"INFO: CWD is now: {os.getcwd()}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to chdir to {target_cwd_for_eco}: {e}"); self.set_controls_state('idle'); return
            
            self.processing_thread = threading.Thread(target=run_eco_logic, args=eco_logic_args_tuple, daemon=True)
            self.processing_thread.start()
        else:
            self.update_status_display(f"INFO: BOB 'run'/'repo' subdirs not in {repo_area_abs}. Starting 'bob wa create' setup...")
            self.update_summary_display("Repo Area setup required...")
            
            setup_specific_params_tuple = (repo_area_abs, params["ip"], params["block"], params["chip"], params["process"])
            
            self.setup_thread = threading.Thread(
                target=self.run_workspace_setup,
                args=(setup_specific_params_tuple, eco_logic_args_tuple),
                daemon=True)
            self.setup_thread.start()

    def _get_and_validate_params(self):
        """Gathers and validates parameters from the GUI."""
        params = {
            "repo_area": self.repo_area_var.get().strip(),
            "eco_work_dir_name": self.eco_work_dir_name_var.get().strip(),
            "ip": self.ip_var.get().strip(),
            "chip": self.chip_var.get().strip(),
            "process": self.process_var.get().strip(),
            "block": self.block_var.get().strip(),
            "tool": self.tool_var.get().strip(),
            "dsa_enabled": self.dsa_var.get(),
            "var_file": self.block_specific_var_file_var.get().strip()
        }
        if not all([params["repo_area"], params["eco_work_dir_name"], params["block"], params["tool"]]):
            raise ValueError("Repo Area, ECO Work Dir Name, BLOCK, and TOOL are required.")
        
        try:
            params["num_iterations"], params["analysis_sequences"] = self.parse_analysis_input(self.analysis_input_text.get("1.0", "end-1c"))
            params["timeout_minutes"] = int(self.timeout_var.get())
            params["check_interval"] = int(self.interval_var.get())
        except ValueError:
            raise ValueError("Timeout and Interval must be valid integers.")
        return params

    def _prepare_eco_logic_args(self, params):
        """Prepares the tuple of arguments for the run_eco_logic function."""
        repo_area_abs = os.path.abspath(params["repo_area"])
        eco_run_dir_abs = os.path.join(repo_area_abs, "run", params["eco_work_dir_name"])
        
        return (
            "/google/gchips/workspace/redondo-asia/tpe/user/askakshay/MECO/base_var_file.var",
            params["block"], params["tool"], params["analysis_sequences"], params["num_iterations"],
            eco_run_dir_abs, params["timeout_minutes"], params["check_interval"], params["dsa_enabled"],
            self.update_status_display, self.update_summary_display, self.processing_complete,
            lambda: self.block_specific_var_file_var.get().strip()
        )

    def _setup_logging(self, eco_run_dir_abs, eco_work_dir_name):
        """Creates the run directory and sets up the log file path."""
        if not os.path.exists(eco_run_dir_abs):
            self.update_status_display(f"INFO: Creating ECO work directory: {eco_run_dir_abs}")
            os.makedirs(eco_run_dir_abs)
        self.log_file_path = os.path.join(eco_run_dir_abs, f"{eco_work_dir_name}_{datetime.datetime.now().strftime('%H:%M:%S_%d-%m-%Y')}.log")
        self.update_status_display(f"INFO: Log file will be at: {self.log_file_path}")

    def run_workspace_setup(self, setup_params_tuple, eco_params_tuple_for_callback):
        """Runs 'bob wa create' and handles its outcome."""
        repo_area_abs, ip, block, chip, process = setup_params_tuple
        try:
            self.update_summary_display("Getting username...")
            username_res = run_bob_command(['whoami'], status_updater=self.update_status_display, timeout_minutes=1)
            if not username_res or username_res.returncode != 0 or not username_res.stdout.strip():
                stderr = username_res.stderr.strip() if username_res and username_res.stderr else "N/A"
                raise ValueError(f"'whoami' failed. Code:{username_res.returncode if username_res else 'N/A'}, Err:{stderr}")
            
            username = username_res.stdout.strip()
            email = f"{username}@google.com"
            self.update_status_display(f"INFO: Username: {username}")
            
            cmd = ["bob", "wa", "create", "--area", repo_area_abs, "--ip", ip, "--block", block, "--chip", chip, "--process", process]
            input_str = f"y\n{email}\ny\n"
            
            wa_res = run_bob_command(cmd, input=input_str, status_updater=self.update_status_display, timeout_minutes=5)

            if not wa_res:
                 raise RuntimeError("'bob wa create' command could not be executed.")

            if wa_res.stdout: self.update_status_display(f"INFO: 'bob wa create' stdout:\n{wa_res.stdout.strip()}")
            if wa_res.stderr: self.update_status_display(f"WARN: 'bob wa create' stderr:\n{wa_res.stderr.strip()}")
            if wa_res.returncode != 0:
                raise RuntimeError(f"'bob wa create' failed with exit code {wa_res.returncode}.")
            
            self.update_summary_display("Verifying Repo Area subdirs...")
            time.sleep(1) 
            if not (os.path.isdir(os.path.join(repo_area_abs, "run")) and os.path.isdir(os.path.join(repo_area_abs, "repo"))):
                raise RuntimeError(f"'run' and 'repo' subdirs not found in {repo_area_abs} after 'bob wa create'.")
            
            self.update_status_display("INFO: Repo Area setup successful.")
            self.after(0, self.workspace_setup_complete, eco_params_tuple_for_callback)

        except Exception as e:
            self.update_status_display(f"ERROR: Repo Area setup failed: {e}")
            self.after(0, self.workspace_setup_failed, f"Setup failed: {e}")
        finally:
            self.setup_thread = None

    def workspace_setup_complete(self, eco_params_tuple):
        """Callback for successful workspace setup. Prepares and starts the main ECO logic."""
        if not self.winfo_exists(): return
        
        eco_run_dir_abs = eco_params_tuple[5]
        eco_work_dir_name = os.path.basename(eco_run_dir_abs)
        self._setup_logging(eco_run_dir_abs, eco_work_dir_name)

        try:
            target_cwd_for_eco = os.path.dirname(eco_run_dir_abs) 
            os.chdir(target_cwd_for_eco)
            self.update_status_display(f"INFO: CWD is now: {os.getcwd()}")
        except Exception as e:
            self.workspace_setup_failed(f"Failed to chdir to {target_cwd_for_eco}: {e}")
            return
            
        self.update_summary_display("Repo Area setup complete. Starting ECO run...")
        self.set_controls_state('running')
        self.processing_thread = threading.Thread(target=run_eco_logic, args=eco_params_tuple, daemon=True)
        self.processing_thread.start()

    def workspace_setup_failed(self, error_message):
        """Callback for failed workspace setup."""
        if not self.winfo_exists(): return
        messagebox.showerror("Repo Area Setup Failed", f"Could not set up Repo Area.\nError: {error_message}")
        self.update_summary_display("Repo Area setup failed.")
        self.set_controls_state('idle')

    def update_summary_display(self, message):
        self.summary_var.set(message)

    def parse_analysis_input(self, text):
        raw_text = text.strip() or DEFAULT_ANALYSIS_INPUT

        # --- MODIFIED: Custom parser for nested braces ---
        def parse(s):
            """
            Parses a string for top-level curly-braced groups.
            """
            groups = []
            level = 0
            start_index = -1
            for i, char in enumerate(s):
                if char == '{':
                    if level == 0:
                        start_index = i
                    level += 1
                elif char == '}':
                    level -= 1
                    if level == 0 and start_index != -1:
                        groups.append(s[start_index:i+1])
                        start_index = -1
            return groups

        top_level_groups = parse(raw_text)

        final_groups = []
        for group in top_level_groups:
            # Remove the outer braces to inspect the content
            inner_content = group[1:-1].strip()
            
            # Check if the inner content itself contains braced groups (i.e., is nested)
            if inner_content.startswith('{') and inner_content.endswith('}'):
                nested_groups = parse(inner_content)
                # Ensure that the entire inner content is composed of these nested groups
                if "".join(nested_groups).replace(" ", "") == inner_content.replace(" ", ""):
                    cleaned_nested_groups = [g[1:-1].strip() for g in nested_groups]
                    final_groups.append(cleaned_nested_groups)
                else:
                     final_groups.append(group) # Not a clean nested structure, treat as a single group
            else:
                final_groups.append(group)

        # Validate leakage
        for group in final_groups:
            if isinstance(group, str): # e.g., '{leakage}'
                words = group[1:-1].split() # remove braces before splitting
                if "leakage" in words and len(words) > 1:
                    messagebox.showerror("Invalid Analysis Input", "Leakage cannot be clubbed with other stages. Please put {leakage} in its own iteration.")
                    raise ValueError("Invalid leakage configuration")
            elif isinstance(group, list): # e.g., ['max_tran_eco', 'setup_eco']
                 for sub_group in group:
                     if "leakage" in sub_group.split():
                          messagebox.showerror("Invalid Analysis Input", "Leakage cannot be part of a multi-stage (nested) iteration.")
                          raise ValueError("Invalid leakage configuration")

        return len(final_groups), final_groups



    def save_configuration(self):
        cfg = { "repo_area": self.repo_area_var.get(), "eco_work_dir_name": self.eco_work_dir_name_var.get(),
                "ip_name": self.ip_var.get(), "chip_name": self.chip_var.get(), "process_name": self.process_var.get(),
                "block_name": self.block_var.get(), "tool_name": self.tool_var.get(),
                "dsa_enabled": self.dsa_var.get(),
                "block_specific_var_file": self.block_specific_var_file_var.get(),
                "analysis_sequences_name": self.analysis_input_text.get("1.0", "end-1c"),
                "timeout_minutes": self.timeout_var.get(),
                "check_interval_seconds": self.interval_var.get() }
        fname = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Config","*.json")])
        if fname:
            try:
                with open(fname,'w') as f: json.dump(cfg,f,indent=4)
                self.summary_var.set(f"Configuration saved to {os.path.basename(fname)}")
            except Exception as e:
                messagebox.showerror("Save Error", f"Failed to save configuration file: {e}")

    def load_configuration(self):
        fname = filedialog.askopenfilename(filetypes=[("JSON Config","*.json")])
        if fname:
            try:
                with open(fname,'r') as f: cfg = json.load(f)
                self.repo_area_var.set(cfg.get("repo_area", DEFAULT_SETUP_REPO_AREA))
                self.eco_work_dir_name_var.set(cfg.get("eco_work_dir_name", DEFAULT_ECO_WORK_DIR_NAME))
                self.ip_var.set(cfg.get("ip_name", DEFAULT_IP_NAME))
                self.chip_var.set(cfg.get("chip_name",DEFAULT_CHIP_NAME))
                self.process_var.set(cfg.get("process_name",DEFAULT_PROCESS_NODE))
                self.block_var.set(cfg.get("block_name",""))
                self.tool_var.set(cfg.get("tool_name","FC"))
                self.dsa_var.set(cfg.get("dsa_enabled", False))
                self.block_specific_var_file_var.set(cfg.get("block_specific_var_file",""))
                self.analysis_input_text.delete("1.0", "end"); self.analysis_input_text.insert("1.0", cfg.get("analysis_sequences_name", DEFAULT_ANALYSIS_INPUT))
                self.timeout_var.set(cfg.get("timeout_minutes", str(DEFAULT_BOB_RUN_TIMEOUT_MINUTES)))
                self.interval_var.set(cfg.get("check_interval_seconds", str(DEFAULT_BOB_CHECK_INTERVAL_SECONDS)))
                self.summary_var.set(f"Loaded configuration from {os.path.basename(fname)}")
            except Exception as e:
                messagebox.showerror("Load Error", f"Failed to load or parse configuration file: {e}")

    def quit_app(self):
        if (self.processing_thread and self.processing_thread.is_alive()) or \
           (self.setup_thread and self.setup_thread.is_alive()):
            if messagebox.askyesno("Confirm Quit", "A process is running. Quitting will abort the process. Quit anyway?"):
                self.abort_processing()
                self.after(1000, self.destroy)
        else:
            self.destroy()

if __name__ == "__main__":
    app = EcoRunnerApp()
    app.protocol("WM_DELETE_WINDOW", app.quit_app)
    app.mainloop()
