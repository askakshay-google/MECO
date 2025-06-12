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
DEFAULT_SETUP_REPO_AREA = "MECO_REPO"
DEFAULT_ECO_WORK_DIR_NAME = "Trial1_meco"
DEFAULT_CHIP_NAME = "lajolla"
DEFAULT_PROCESS_NODE = "n2p"
DEFAULT_IP_NAME = "hsio"
DEFAULT_BOB_RUN_TIMEOUT_MINUTES = 720
DEFAULT_BOB_CHECK_INTERVAL_SECONDS = 60
# --- End Default Configuration --- #

# Constant for key nodes to check for VALID status per stage. Wildcards are supported.
KEY_NODES_PER_STAGE = {
    "pdp": "pdp/dummyfill.merge",
    "pex": "pex/pex.starrc",
    "sta": "sta/sta.bb_sta.*",
    "pceco": "pceco/pceco",
    "pnr": "pnr/chipfinish",
    "applyeco": "pnr/chipfinish"
}

DEFAULT_BASE_VAR_FILE_NAME = "base_var_file.var" # This file should exist in the script's directory

DEFAULT_ANALYSIS_INPUT = "{max_tran_eco max_cap_eco} {hold_eco} {hold_eco} {setup_eco} {max_tran_eco max_cap_eco}"

MAIN_ITER_STAGES = ["pdp", "pex", "sta", "pceco", "applyeco"]
LAST_ITER_STAGES = ["pdp", "pex", "sta"]

abort_flag = threading.Event()
continue_event = threading.Event()

failed_info = {
    "is_failed_state": False,
    "branch": None,
    "node_pattern": None,
    "specific_node": None
}

def get_script_directory():
    """Returns the directory where the current Python script is located."""
    return os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# Core BOB Logic Functions
# =============================================================================

def run_bob_command(command, work_dir=".", status_updater=None, summary_updater=None, timeout_minutes=DEFAULT_BOB_RUN_TIMEOUT_MINUTES):
    if abort_flag.is_set():
        if status_updater: status_updater("INFO: Abort requested. Skipping command.")
        return False

    cmd_str = ' '.join(command) if isinstance(command, list) else command
    if status_updater: status_updater(f"RUNNING: {cmd_str}  (in {work_dir})")

    try:
        timeout_secs = timeout_minutes * 60 if timeout_minutes > 0 else None

        result = subprocess.run(
            command, cwd=work_dir, check=False, shell=isinstance(command, str),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', errors='ignore', timeout=timeout_secs
        )

        if result.returncode != 0:
            err_msg = (f"ERROR: Command failed: {cmd_str}\n"
                       f"  Return code: {result.returncode}\n"
                       f"  Stderr: {result.stderr.strip()}\n")
            if status_updater: status_updater(err_msg)
            return False
        return True
    except subprocess.TimeoutExpired:
        err_msg = f"ERROR: Command timed out after {timeout_minutes} minutes: {cmd_str}"
        if status_updater: status_updater(err_msg)
        return False
    except FileNotFoundError:
        cmd_name = command[0] if isinstance(command, list) else command.split()[0]
        err_msg = f"ERROR: Command '{cmd_name}' not found. Is 'bob' in your PATH?"
        if status_updater: status_updater(err_msg)
        return False
    except Exception as e:
        cmd_name = command[0] if isinstance(command, list) else command.split()[0]
        err_msg = f"ERROR: Unexpected error running {cmd_str}: {e}"
        if status_updater: status_updater(err_msg)
        return False

def wait_for_bob_run(run_area, branch, node_pattern, timeout_minutes, check_interval_seconds, status_updater=None, summary_updater=None, is_single_node_check=False):
    log_prefix = f"WAIT({node_pattern} in {branch})"
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60 if timeout_minutes > 0 else float('inf')

    if not os.path.isdir(run_area):
        msg = f"ERROR: {log_prefix}: ECO Run directory does not exist: {run_area}"
        if status_updater: status_updater(msg)
        return "ERROR", {}, msg

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    safe_node_pattern_for_file = re.sub(r'[\\/*?"<>|:]', '_', node_pattern)
    temp_log_filename = f"bob_info_{branch}_{safe_node_pattern_for_file}_{timestamp}.json"
    log_file_path = os.path.join(tempfile.gettempdir(), temp_log_filename)

    last_statuses_summary = None
    terminal_states = {"VALID", "FAILED", "INVALID", "ERROR", "KILLED"}
    all_final_statuses = {}

    while True:
        if abort_flag.is_set():
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
            result = subprocess.run(bob_info_cmd, check=False, shell=False, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='ignore', cwd=run_area)
            if result.returncode != 0:
                if status_updater: status_updater(f"WARN: {log_prefix}: 'bob info' failed. Retrying... Stderr: {result.stderr.strip()}")
                time.sleep(check_interval_seconds); continue
        except Exception as e:
            if status_updater: status_updater(f"ERROR: 'bob info' command failed: {e}")
            return "ERROR", {}, f"'bob info' command failed: {e}"

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
                # Change 1: Show branch in status bar
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
                           base_var_path, block_name, tool_name, status_updater=None):
    if status_updater:
        status_updater(f"DEBUG: --- Entering create_final_var_file for branch '{current_branch}' ---")
        status_updater(f"DEBUG: eco_work_dir_abs: {eco_work_dir_abs}")
        status_updater(f"DEBUG: base_var_path: {base_var_path}")
        status_updater(f"DEBUG: block_specific_var_path: {block_specific_var_path}")

    op, t, t_name, rec = ("ndm", "icc2", "fc", "bbrecipe_apply setup_fc") if tool_name == "FC" else (("enc.dat", "invs", "innovus", "#Invs flow") if tool_name == "Innovus" else (None, None, None, None))
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

        bbsets = f"""\n# --- Auto-gen settings by ECO script for branch: {current_branch} ---
bbset hierarchy.{block_name}.chipfinish.source {{{chipfinish_source_dir}}}
bbset pnr.Tool {t_name}
bbset Tool(pnr) {t_name}
{rec}
bbset pex.FillOasisFiles {{{os.path.join(pre_dir_context, 'pdp/dummyfill/outs', f'{block_name}.dummyfill.beol.oas')}}}
bbset pex.source {{{os.path.join(pre_dir_context, 'pex')}}}
bbset pnr.applyeco.InputDatabase {{{os.path.join(chipfp_dir, 'outs', f'{block_name}.{op}')}}}
bbset pnr.applyeco.ECOChangeFile {{{os.path.join(pre_dir_context, 'pceco/pceco/outs', f'eco.{t}.tcl')}}}
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
    if current_iter_name != "Last_iter":
        try:
            analysis_index = 0 if current_iter_name == "main" else int(current_iter_name.split('_')[1])
            if 0 <= analysis_index < len(analysis_sequences_name):
                specific_iteration_bbsets = f"\nbbset pceco.EcoOrder {{SMSA1}}\nbbset pceco.SMSA1 {analysis_sequences_name[analysis_index]}\n"
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
                  timeout_minutes, check_interval,
                  status_updater, summary_updater, completion_callback, get_gui_block_specific_var_file_func):
    global failed_info
    current_stage_for_error, current_branch_for_error, process_outcome = "setup", "N/A", "UNKNOWN"

    try:
        summary_updater("Starting ECO process...")
        status_updater(f"INFO: Starting ECO for BLOCK {block_name}, TOOL {tool_name}")
        if not os.path.exists(eco_work_dir): os.makedirs(eco_work_dir)

        all_iterations = ["main"] + [f"Iter_{i}" for i in range(1, num_iterations)] + (["Last_iter"] if num_iterations >=1 else [])
        prev_iter = ""
        block_specific_var_path_for_run = get_gui_block_specific_var_file_func()

        for i, current_iter_name in enumerate(all_iterations):
            current_branch_for_error = current_iter_name
            stages_to_run = MAIN_ITER_STAGES if current_iter_name != "Last_iter" else LAST_ITER_STAGES

            summary_updater(f"Branch '{current_iter_name}': Preparing...")
            status_updater(f"\n{'='*20} Prep Branch: '{current_iter_name}' {'='*20}")
            if abort_flag.is_set(): raise RuntimeError("Aborted")

            current_stage_for_error = f"prepare_var_{current_iter_name}"
            final_var_file = create_final_var_file(eco_work_dir, current_iter_name, prev_iter, block_specific_var_path_for_run, base_var_file_path, block_name, tool_name, status_updater)
            if not final_var_file or not _add_analysis_bbsets_to_var_file(final_var_file, current_iter_name, analysis_sequences_name, status_updater):
                raise RuntimeError(f"FAIL: Prepare var file for '{current_iter_name}'. See log for details (e.g., missing 'chipfinish.source' in block specific var file).")

            current_stage_for_error = f"create_{current_iter_name}"
            create_cmd = ["bob", "create", "-r", eco_work_dir, "-v", final_var_file, "-s"] + stages_to_run
            if current_iter_name != "main": create_cmd[2:2] = ["--branch", current_iter_name]
            if not run_bob_command(create_cmd, work_dir=eco_work_dir, status_updater=status_updater):
                raise RuntimeError(f"FAIL: 'bob create' for '{current_iter_name}'.")

            stage_idx = 0
            while stage_idx < len(stages_to_run):
                stage_type = stages_to_run[stage_idx]
                run_node_pattern = f"{stage_type}/*"
                current_stage_for_error = f"run_{current_iter_name}_{stage_type}"
                if abort_flag.is_set(): raise RuntimeError("Aborted")

                summary_updater(f"Branch '{current_iter_name}': Running stage '{stage_type}'")
                run_cmd = ["bob", "run", "-r", eco_work_dir, "--branch", current_iter_name, "--node", run_node_pattern]
                if not run_bob_command(run_cmd, work_dir=eco_work_dir, status_updater=status_updater):
                    raise RuntimeError(f"'bob run' command failed for {run_node_pattern}")

                summary_updater(f"Branch '{current_iter_name}': Waiting for stage '{stage_type}'")
                wait_status, all_statuses, wait_msg = wait_for_bob_run(eco_work_dir, current_iter_name, run_node_pattern, timeout_minutes, check_interval, status_updater, summary_updater)
                if wait_status != "COMPLETED":
                    raise RuntimeError(f"{wait_status} waiting for {stage_type}: {wait_msg}")

                key_pattern = KEY_NODES_PER_STAGE.get(stage_type)
                failed_key_node = next((name for name, status in all_statuses.items() if status != "VALID" and fnmatch.fnmatch(name, key_pattern or "")), None)

                if failed_key_node:
                    while True: # Key Node Failure Loop
                        failed_info.update({"is_failed_state": True, "branch": current_iter_name, "node_pattern": key_pattern, "specific_node": failed_key_node})
                        summary_updater(f"Branch '{current_iter_name}': FAILED at '{failed_key_node}'. User action needed.")
                        process_outcome="FAILED"; completion_callback(process_outcome)
                        status_updater(f"ERROR: Key node '{failed_key_node}' failed. User action required.")
                        continue_event.wait(); continue_event.clear()
                        if abort_flag.is_set(): raise RuntimeError("Aborted")

                        # Change 2: Handle updated block specific var file
                        new_block_specific_path = get_gui_block_specific_var_file_func()
                        if new_block_specific_path != block_specific_var_path_for_run:
                            status_updater("INFO: New block-specific var file detected. Re-creating final var file...")
                            summary_updater(f"Branch '{current_iter_name}': Updating var file...")
                            block_specific_var_path_for_run = new_block_specific_path
                            new_var_file = create_final_var_file(eco_work_dir, current_iter_name, prev_iter, block_specific_var_path_for_run, base_var_file_path, block_name, tool_name, status_updater)
                            if not new_var_file or not _add_analysis_bbsets_to_var_file(new_var_file, current_iter_name, analysis_sequences_name, status_updater):
                                status_updater("ERROR: Failed to recreate var file. Please try again."); summary_updater("Error creating var file."); continue

                            status_updater("INFO: Updating flow.var for pending nodes...")
                            for s_idx in range(stage_idx, len(stages_to_run)):
                                pending_stage_dir = os.path.join(eco_work_dir, current_iter_name, stages_to_run[s_idx])
                                if not os.path.isdir(pending_stage_dir): continue
                                for node_dir in os.listdir(pending_stage_dir):
                                    flow_var_path = os.path.join(pending_stage_dir, node_dir, "vars", "flow.var")
                                    if os.path.isfile(flow_var_path):
                                        try:
                                            old_var_path = os.path.join(pending_stage_dir, node_dir, "vars", "flow.var_old")
                                            shutil.move(flow_var_path, old_var_path)
                                            shutil.copy(new_var_file, flow_var_path)
                                            status_updater(f"  - Updated: {flow_var_path}")
                                        except Exception as e: status_updater(f"ERROR: Failed updating {flow_var_path}: {e}")

                        failed_info["is_failed_state"] = False
                        key_node_for_stage = KEY_NODES_PER_STAGE.get(stage_type)
                        summary_updater(f"Branch '{current_iter_name}': Continue pressed, re-running node: {key_node_for_stage}")
                        
                        rerun_key_node_cmd = ["bob", "run", "-r", eco_work_dir, "--branch", current_iter_name, "--node", key_node_for_stage, "-f"]
                        
                        if not run_bob_command(rerun_key_node_cmd, work_dir=eco_work_dir, status_updater=status_updater):
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
                else:
                    for name, status in all_statuses.items():
                        if status != "VALID" and not (key_pattern and fnmatch.fnmatch(name, key_pattern)):
                            status_updater(f"WARN: Non-key node '{name}' has status '{status}'. Continuing.")
                    stage_idx += 1

            prev_iter = current_iter_name
        process_outcome = "SUCCESS"
        summary_updater("All iterations completed successfully.")
        status_updater(f"\n{'='*20} All iterations COMPLETED successfully. {'='*20}")
    except Exception as e:
        process_outcome = "ERROR" if not abort_flag.is_set() else "ABORTED"
        summary_updater(f"PROCESS {process_outcome}!")
        err_msg = f"\n{'!'*20} ECO PROCESS {process_outcome} {'!'*20}\n"
        err_msg += f"ERROR @ Branch: {current_branch_for_error}, Stage: {current_stage_for_error}\nDETAILS: {e}\n"
        status_updater(err_msg)
    finally:
        if not failed_info.get("is_failed_state", False):
             completion_callback(process_outcome)

class EcoRunnerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multi-ECO Utility (v1.08)")
        self.geometry("850x650")
        self.processing_thread = None
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
        self.repo_area_entry = ttk.Entry(config_frame, textvariable=self.repo_area_var, width=60)
        self.repo_area_entry.grid(row=current_row, column=1, columnspan=2, padx=5, pady=3, sticky=tk.EW)
        self.repo_area_browse = ttk.Button(config_frame, text="Browse...", command=lambda: self.browse_directory(self.repo_area_var), style="File.TButton"); self.repo_area_browse.grid(row=current_row, column=3, padx=5, pady=3); current_row+=1

        ttk.Label(config_frame, text="IP Name:", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W); self.ip_var = tk.StringVar(value=DEFAULT_IP_NAME); self.ip_entry = ttk.Entry(config_frame, textvariable=self.ip_var, width=30); self.ip_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W);
        ttk.Label(config_frame, text="Chip Name:", foreground="darkgreen").grid(row=current_row, column=2, padx=(20,5), pady=3, sticky=tk.W); self.chip_var = tk.StringVar(value=DEFAULT_CHIP_NAME); self.chip_entry = ttk.Entry(config_frame, textvariable=self.chip_var, width=30); self.chip_entry.grid(row=current_row, column=3, padx=5, pady=3, sticky=tk.W); current_row+=1
        ttk.Label(config_frame, text="Process:", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W); self.process_var = tk.StringVar(value=DEFAULT_PROCESS_NODE); self.process_entry = ttk.Entry(config_frame, textvariable=self.process_var, width=30); self.process_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W); current_row+=1

        ttk.Label(config_frame, text="ECO Work Dir Name :", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.eco_work_dir_name_var = tk.StringVar(value=DEFAULT_ECO_WORK_DIR_NAME)
        self.eco_work_dir_name_entry = ttk.Entry(config_frame, textvariable=self.eco_work_dir_name_var, width=60)
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
        log_msg = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n"
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
        is_running, is_failed, is_idle = (mode == 'running'), (mode == 'failed'), (mode == 'idle')
        state_map = {'idle': tk.NORMAL, 'running': tk.DISABLED, 'failed': tk.DISABLED}
        idle_state = state_map[mode]

        for widget in [self.start_button, self.clear_button, self.load_config_button, self.save_config_button,
                       self.repo_area_entry, self.repo_area_browse, self.ip_entry, self.chip_entry,
                       self.process_entry, self.eco_work_dir_name_entry, self.block_entry, self.timeout_entry,
                       self.interval_entry, self.analysis_input_text]:
            widget.config(state=idle_state)

        self.tool_combo.config(state="readonly" if is_idle else tk.DISABLED)
        self.block_specific_var_file_entry.config(state=tk.NORMAL if is_idle or is_failed else tk.DISABLED)
        self.block_specific_var_file_browse.config(state=tk.NORMAL if is_idle or is_failed else tk.DISABLED)
        self.continue_button.config(state=tk.NORMAL if is_failed else tk.DISABLED)
        self.abort_button.config(state=tk.NORMAL if is_running or is_failed else tk.DISABLED)

    def processing_complete(self, outcome):
        self.processing_thread = None
        self.update_status_display(f"PROCESS: Outcome = {outcome}")

        if outcome == "SUCCESS":
            self.summary_var.set("SUCCESS: ECO process completed.")
            messagebox.showinfo("Success", "ECO process finished successfully.")
        elif outcome == "FAILED":
            self.summary_var.set(f"FAILED: Awaiting user action for node: {failed_info.get('specific_node','N/A')}")
            messagebox.showwarning("Failed", f"Run failed @ key node: {failed_info.get('specific_node','N/A')}\nFix issue (check log at {self.log_file_path}), optionally update var file, then click CONTINUE or ABORT.")
        else:
            self.summary_var.set(f"Process ended with status: {outcome}")
            messagebox.showerror("Error", f"Process ended with status: {outcome}\nCheck log at {self.log_file_path} for details.")

        self.set_controls_state('failed' if outcome == "FAILED" else 'idle')
        if outcome != "FAILED": abort_flag.clear()

    def continue_processing(self):
        if failed_info.get("is_failed_state"):
            self.set_controls_state('running'); continue_event.set()

    def abort_processing(self):
        self.abort_button.config(state=tk.DISABLED)
        self.summary_var.set("Aborting process...")
        abort_flag.set(); continue_event.set()

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

    def start_processing(self):
        self.set_controls_state('running')
        self.status_text.config(state=tk.NORMAL); self.status_text.delete('1.0', tk.END); self.status_text.config(state=tk.DISABLED)
        abort_flag.clear(); continue_event.clear(); failed_info['is_failed_state'] = False

        try:
            repo_area = os.path.abspath(self.repo_area_var.get().strip())
            run_name = self.eco_work_dir_name_var.get().strip()
            eco_run_dir = os.path.join(repo_area, "run", run_name)

            self.log_file_path = os.path.join(eco_run_dir, f"{run_name}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.log")
            if not os.path.exists(eco_run_dir): os.makedirs(eco_run_dir)

            num_iter, analyses = self.parse_analysis_input(self.analysis_input_text.get("1.0", "end-1c"))

            try:
                timeout_mins = int(self.timeout_var.get())
                check_secs = int(self.interval_var.get())
            except ValueError:
                messagebox.showerror("Input Error", "Timeout and Interval must be valid integers.")
                self.set_controls_state('idle')
                return

            eco_logic_args = (
                os.path.join(get_script_directory(), DEFAULT_BASE_VAR_FILE_NAME),
                self.block_var.get(), self.tool_var.get(), analyses, num_iter,
                eco_run_dir, timeout_mins, check_secs,
                self.update_status_display, self.summary_var.set, self.processing_complete,
                lambda: self.block_specific_var_file_var.get().strip()
            )
            self.processing_thread = threading.Thread(target=run_eco_logic, args=eco_logic_args, daemon=True)
            self.processing_thread.start()
        except Exception as e:
            messagebox.showerror("Startup Error", str(e))
            self.set_controls_state('idle')

    def parse_analysis_input(self, text):
        groups = re.findall(r'{[^}]+}', text.strip() or DEFAULT_ANALYSIS_INPUT)
        return len(groups), groups

    def save_configuration(self):
        cfg = { "repo_area": self.repo_area_var.get(), "eco_work_dir_name": self.eco_work_dir_name_var.get(),
                "ip_name": self.ip_var.get(), "chip_name": self.chip_var.get(), "process_name": self.process_var.get(),
                "block_name": self.block_var.get(), "tool_name": self.tool_var.get(),
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
                self.block_specific_var_file_var.set(cfg.get("block_specific_var_file",""))
                self.analysis_input_text.delete("1.0", "end"); self.analysis_input_text.insert("1.0", cfg.get("analysis_sequences_name", DEFAULT_ANALYSIS_INPUT))
                self.timeout_var.set(cfg.get("timeout_minutes", str(DEFAULT_BOB_RUN_TIMEOUT_MINUTES)))
                self.interval_var.set(cfg.get("check_interval_seconds", str(DEFAULT_BOB_CHECK_INTERVAL_SECONDS)))
                self.summary_var.set(f"Loaded configuration from {os.path.basename(fname)}")
            except Exception as e:
                messagebox.showerror("Load Error", f"Failed to load or parse configuration file: {e}")

    def quit_app(self):
        if self.processing_thread and self.processing_thread.is_alive():
            if messagebox.askyesno("Confirm Quit", "A process is running. Quitting will abort the process. Quit anyway?"):
                self.abort_processing(); self.after(500, self.destroy)
        else: self.destroy()

if __name__ == "__main__":
    app = EcoRunnerApp()
    app.protocol("WM_DELETE_WINDOW", app.quit_app)
    app.mainloop()
