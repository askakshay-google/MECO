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
#******kumkumn******
import re

# --- Default Project/Flow Configuration --- #
DEFAULT_SETUP_REPO_AREA = "MECO_REPO" # Renamed from DEFAULT_SETUP_WORK_AREA
DEFAULT_ECO_WORK_DIR_NAME = "Trial1_meco" # Default name for the dir inside <RepoArea>/run/
DEFAULT_CHIP_NAME = "lajolla"
DEFAULT_PROCESS_NODE = "n2p"
DEFAULT_IP_NAME = "hsio"
DEFAULT_BOB_RUN_TIMEOUT_MINUTES = 720
DEFAULT_BOB_CHECK_INTERVAL_SECONDS = 60
# --- End Default Configuration --- #

#******kumkumn******
DEFAULT_BASE_VAR_FILE_NAME = "base_var_file.var" # This file should exist in the script's directory
DEFAULT_RUN_VAR_FILE_NAME = "final_var_file.var" #for the output var file name, this is used by the script to make a new var file using the default and user provided files

# Default for the new analysis field
# 2 tran/cap 2 setup 2 hold and 1 tran/cap
DEFAULT_ANALYSIS_INPUT = "{max_tran_eco max_cap_eco} {hold_eco} {hold_eco} {setup_eco} {max_tran_eco max_cap_eco}"

#exit pattern for node_pattern
#if node_pattern has these keywords, comeout of wait_for_bob_run
SPECIAL_EXIT_PATTERNS = ["export", "assemble", "macro", "end", "join", "summary"]


MAIN_ITER_STAGES = ["pdp", "pex", "sta", "pceco", "pnr"]
LAST_ITER_STAGES = ["pdp", "pex", "sta"]

abort_flag = threading.Event()
continue_event = threading.Event()

failed_info = {
    "is_failed_state": False,
    "branch": None,
    "node_pattern": None, 
    "specific_node": None 
}

#******kumkumn******
# Helper function to get the script directory
#__file__ returns script's relative path
def get_script_directory():
    """Returns the directory where the current Python script is located."""
    return os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# Core BOB Logic Functions
# =============================================================================

def run_bob_command(command, work_dir=".", status_updater=None, summary_updater=None, check=True, timeout_minutes=DEFAULT_BOB_RUN_TIMEOUT_MINUTES):
    if abort_flag.is_set():
        if status_updater: status_updater("INFO: Abort requested. Skipping command.")
        return False

    cmd_str = ' '.join(command) if isinstance(command, list) else command
    is_bob_command = isinstance(command, list) and command and command[0] == "bob"

    if status_updater: status_updater(f"RUNNING: {cmd_str}  (in {work_dir})")

    if is_bob_command and len(command) > 1:
        summary_action = None
        bob_action = command[1]
        if bob_action == "create" and "-r" in command: # -r points to the ECO Work Dir
            try:
                r_index = command.index("-r") + 1
                # Summary shows the ECO work dir being created/updated
                summary_action = f"Creating/Updating ECO Dir '{os.path.basename(command[r_index])}'..."
            except (ValueError, IndexError): pass
        elif bob_action == "start":
            summary_action = f"Starting Run..."
        elif bob_action == "run" and "--node" in command:
            try:
                node_index = command.index("--node") + 1
                summary_action = f"Running node '{command[node_index]}'"
            except (ValueError, IndexError): pass
        if summary_updater and summary_action:
            summary_updater(summary_action)
    try:
        timeout_secs = timeout_minutes * 60 if timeout_minutes > 0 else None
        use_shell = isinstance(command, str)

        result = subprocess.run(
            command, cwd=work_dir, check=False, shell=use_shell,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', errors='ignore', timeout=timeout_secs
        )

        if result.returncode != 0:
            err_msg = (f"ERROR: Command failed: {cmd_str}\n"
                       f"  Return code: {result.returncode}\n"
                       f"  Stderr: {result.stderr.strip()}\n"
                       f"  Stdout: {result.stdout.strip()}")
            if status_updater: status_updater(err_msg)
            cmd_name_for_summary = command[1] if is_bob_command and len(command) > 1 else (command[0] if isinstance(command,list) else command.split()[0])
            if summary_updater and cmd_name_for_summary != "info": 
                summary_updater(f"Command Failed: {cmd_name_for_summary}...")
            return False
        return True
    except subprocess.TimeoutExpired:
        err_msg = f"ERROR: Command timed out after {timeout_minutes} minutes: {cmd_str}"
        if status_updater: status_updater(err_msg)
        cmd_name_for_summary = command[1] if is_bob_command and len(command) > 1 else (command[0] if isinstance(command,list) else command.split()[0])
        if summary_updater and cmd_name_for_summary != "info":
             summary_updater(f"Command Timed Out: {cmd_name_for_summary}...")
        return False
    except FileNotFoundError:
        cmd_name = command[0] if isinstance(command, list) else command.split()[0]
        err_msg = f"ERROR: Command '{cmd_name}' not found. Is 'bob' in your PATH?"
        if status_updater: status_updater(err_msg)
        if summary_updater: summary_updater(f"Command Not Found: {cmd_name}")
        return False
    except Exception as e:
        cmd_name = command[0] if isinstance(command, list) else command.split()[0]
        err_msg = f"ERROR: Unexpected error running {cmd_str}: {e}"
        if status_updater: status_updater(err_msg)
        if summary_updater: summary_updater(f"Script Error during command: {cmd_name}")
        return False

def wait_for_bob_run(run_area, branch, node_pattern, timeout_minutes, check_interval_seconds, status_updater=None, summary_updater=None):
    run_base = os.path.basename(run_area) # run_area is the ECO Work Dir
    log_prefix = f"WAIT({node_pattern} in {branch})"
    stage_name_summary = node_pattern.split('/')[0] if '/' in node_pattern else node_pattern
    
    #******kumkum******
    # --- Use the globally defined SPECIAL_EXIT_PATTERNS ---
    for pattern in SPECIAL_EXIT_PATTERNS:
        if pattern in node_pattern.lower(): # Use .lower() for case-insensitive check
            msg = f"INFO: {log_prefix}: Node pattern '{node_pattern}' contains '{pattern}'. Exiting wait_for_bob_run gracefully."
            if status_updater: status_updater(msg)
            if summary_updater: summary_updater(f"Skipping wait: {stage_name_summary} (contains '{pattern}')")
            return "VALID", msg, None # Return a special status indicating it was skipped

    if status_updater: status_updater(f"{log_prefix}: Waiting for nodes (parsing JSON)...")
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60 if timeout_minutes > 0 else float('inf')

    if not os.path.isdir(run_area): # Check if ECO Work Dir exists
        msg = f"ERROR: {log_prefix}: ECO Run directory does not exist: {run_area}"
        if status_updater: status_updater(msg); return "ERROR", msg, None

    pid = os.getpid(); timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    safe_node_pattern_for_file = re.sub(r'[\\/*?"<>|:]', '_', node_pattern)
    temp_log_filename = f"bob_info_{branch}_{safe_node_pattern_for_file}_{timestamp}.json"
    # Log file should be inside the ECO work dir (run_area)
    log_file_path = os.path.join(run_area, temp_log_filename) 

    last_statuses_summary = None; cleanup_log = True
    failure_states = {"FAILED", "INVALID", "ERROR", "KILLED"}

    match_func = (lambda jn: jn is not None and jn.startswith(node_pattern[:-1])) if node_pattern.endswith('/*') \
                 else (lambda jn: jn == node_pattern)

    while True:
        if abort_flag.is_set():
            msg = f"INFO: {log_prefix}: Abort requested."
            if status_updater: status_updater(msg)
            if summary_updater: summary_updater(f"Aborted: {stage_name_summary}")
            if os.path.exists(log_file_path):
                try: os.remove(log_file_path)
                except OSError: 
                    if status_updater: status_updater(f"WARN: Failed to remove temp log {log_file_path} on abort.")
            return "ABORTED", msg, None

        elapsed_minutes = int((time.time() - start_time) / 60)
        current_summary_disp = last_statuses_summary if last_statuses_summary else "Polling"
        if summary_updater: summary_updater(f"Branch: {branch}, Stage: {stage_name_summary}, Status: {current_summary_disp}, Elap: {elapsed_minutes}m")

        if (time.time() - start_time) > timeout_seconds:
            msg = f"ERROR: {log_prefix}: TIMEOUT after {timeout_minutes}m."
            if status_updater: status_updater(msg)
            if summary_updater: summary_updater(f"TIMEOUT: {stage_name_summary} in {branch}")
            if os.path.exists(log_file_path):
                try: os.remove(log_file_path)
                except OSError:
                    if status_updater: status_updater(f"WARN: Failed to remove temp log {log_file_path} on timeout.")
            return "TIMEOUT", msg, None

        bob_info_cmd = ["bob", "info", "-r", run_area, "--branch", branch, "-o", log_file_path]
        try:
            if os.path.exists(log_file_path):
                try: os.remove(log_file_path)
                except OSError as e: 
                    if status_updater: status_updater(f"WARN: Pre-del of {log_file_path} failed: {e}")
            # bob info is run with cwd=run_area (ECO Work Dir) implicitly by subprocess if not specified,
            # or explicitly if run_bob_command was used (but here it's direct subprocess.run)
            result = subprocess.run(bob_info_cmd, check=False, shell=False, timeout=60, 
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                    encoding='utf-8', errors='ignore', cwd=run_area) # Explicitly set cwd for bob info
            if result.returncode != 0:
                if status_updater: status_updater(f"WARN: {log_prefix}: 'bob info' fail (ret {result.returncode}). Retry... Stderr: {result.stderr.strip()}")
                time.sleep(min(5, check_interval_seconds)); continue
        except subprocess.TimeoutExpired: 
            if status_updater: status_updater(f"WARN: {log_prefix}: 'bob info' timeout. Retry...")
            time.sleep(min(5, check_interval_seconds)); continue
        except FileNotFoundError: 
            msg=f"ERROR: {log_prefix}: 'bob' not found."
            if status_updater: status_updater(msg)
            if summary_updater: summary_updater("'bob' not found.")
            return "ERROR", msg, None
        except Exception as e: 
            msg=f"ERROR: {log_prefix}: 'bob info' error: {e}"
            if status_updater: status_updater(msg)
            if summary_updater: summary_updater("bob info error.")
            return "ERROR", msg, None

        statuses_found = {}; specific_failed_node_from_json = None; current_statuses_summary = "Reading status"
        try:
            time.sleep(0.2) 
            with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f: json_content = f.read()
            if not json_content.strip(): 
                if status_updater: status_updater(f"WARN: {log_prefix}: Empty status file. Retry 'bob info'.")
                time.sleep(check_interval_seconds); continue
            jobs_data = json.loads(json_content)
            if not isinstance(jobs_data, list): raise json.JSONDecodeError("Exp list", json_content, 0)

            relevant_found = False
            for job in jobs_data:
                jn, jst, jb = job.get("jobname"), job.get("status","").upper(), job.get("prop.branch")
                if not (jn and jst and jb == branch and match_func(jn)): continue
                relevant_found = True

                # Determine if this job name contains a special exit pattern
                is_special_node_for_skip = False
                for pattern in SPECIAL_EXIT_PATTERNS:
                    if pattern in jn.lower():
                        is_special_node_for_skip = True
                        break

                # --- MODIFIED LOGIC: Only consider it a failure if it's NOT a special skip node ---
                if jst in failure_states and not is_special_node_for_skip:
                    specific_failed_node_from_json = jn
                    msg = f"ERROR: {log_prefix}: Node '{jn}' status {jst}"
                    if status_updater: status_updater(msg)
                    if summary_updater: summary_updater(f"Br: {branch}, St: {stage_name_summary}, Failed@ {specific_failed_node_from_json} ({jst})")
                    cleanup_log = False; return "FAILED", msg, specific_failed_node_from_json
                # --- END MODIFIED LOGIC ---

                statuses_found[jn] = jst # Keep track of statuses for overall check

            if relevant_found:
                # Check if all relevant jobs are either VALID or are special exit nodes (regardless of their reported status)
                all_relevant_jobs_are_valid_or_skipped = True
                for job_name_in_dict, status_in_dict in statuses_found.items():
                    if status_in_dict != "VALID":
                        is_special_exit_job = False
                        for pattern in SPECIAL_EXIT_PATTERNS:
                            if pattern in job_name_in_dict.lower():
                                is_special_exit_job = True
                                break
                        if not is_special_exit_job:
                            all_relevant_jobs_are_valid_or_skipped = False
                            break # Found a non-valid, non-special job that we must wait for

                if all_relevant_jobs_are_valid_or_skipped:
                    msg = f"INFO: {log_prefix}: All {len(statuses_found)} nodes matching '{node_pattern}' are VALID or implicitly SKIPPED."
                    if status_updater: status_updater(msg)
                    if summary_updater: summary_updater(f"Br: {branch}, St: {stage_name_summary}, VALID") # Summary still shows VALID
                    if os.path.exists(log_file_path):
                        try: os.remove(log_file_path)
                        except OSError:
                            if status_updater: status_updater(f"WARN: Failed to remove temp log {log_file_path} on success.")
                    return "VALID", msg, None # Return VALID for success
                current_statuses_summary = ", ".join(sorted(list(set(statuses_found.values()))))
            else: current_statuses_summary = f"Waiting for jobs matching '{node_pattern}'..."
            if current_statuses_summary != last_statuses_summary:
                if status_updater: status_updater(f"INFO: {log_prefix}: Status: {current_statuses_summary}. Found matching '{node_pattern}': {len(statuses_found)}")
                last_statuses_summary = current_statuses_summary
        except FileNotFoundError: 
            if status_updater: status_updater(f"WARN: {log_prefix}: Status file '{log_file_path}' gone. Retry 'bob info'.")
        except json.JSONDecodeError as e: 
            msg=f"ERROR: {log_prefix}: JSON parse fail '{log_file_path}': {e}. Content: '{json_content[:100]}...'"
            if status_updater: status_updater(msg)
            if summary_updater: summary_updater("Status JSON Error.")
            cleanup_log=False; return "JSON_ERROR", msg, None
        except IOError as e: 
            msg=f"ERROR: {log_prefix}: Read fail '{log_file_path}': {e}"
            if status_updater: status_updater(msg)
            if summary_updater: summary_updater("Status Read Error.")
            cleanup_log=False; return "FILE_ERROR", msg, None
        except Exception as e: 
            msg=f"ERROR: {log_prefix}: Proc status error: {e}"
            if status_updater: status_updater(msg)
            if summary_updater: summary_updater("Status Proc Error.")
            cleanup_log=False; return "ERROR", msg, None

        wait_start_time_monotonic = time.monotonic()
        while time.monotonic() - wait_start_time_monotonic < check_interval_seconds:
            if abort_flag.is_set():
                msg=f"INFO: {log_prefix}: Abort in sleep."
                if status_updater: status_updater(msg)
                if summary_updater: summary_updater(f"Aborted: {stage_name_summary}")
                if cleanup_log and os.path.exists(log_file_path):
                    try: os.remove(log_file_path)
                    except OSError: 
                        if status_updater: status_updater(f"WARN: Failed to remove temp log {log_file_path} on abort in sleep.")
                return "ABORTED", msg, None
            time.sleep(0.5)

    if cleanup_log and os.path.exists(log_file_path):
        try: os.remove(log_file_path)
        except OSError: 
            if status_updater: status_updater(f"WARN: Failed to remove temp log {log_file_path} at end of wait.")
    return "ERROR", "Unexpected exit from wait loop.", None


def prepare_iter_var_file(base_var_path, current_branch, prev_branch, eco_work_dir_abs, block_name, tool_name, status_updater=None):
    # eco_work_dir_abs is the absolute path to the specific ECO run directory (e.g. .../RepoArea/run/MyEcoRun1)
    if status_updater: status_updater(f"INFO: Preparing var file for br: {current_branch} (prev: {prev_branch}) using base: {base_var_path}")

    op, t = ("ndm", "fc") if tool_name == "FC" else (("enc.dat", "invs") if tool_name == "Innovus" else (None, None))
    if not op: 
        if status_updater: status_updater(f"ERROR: Invalid TOOL: {tool_name}")
        return None

    chipfp_dir, chipfinish_source_dir = "", ""
    try:
        if not os.path.isfile(base_var_path): raise ValueError(f"Base var file '{base_var_path}' not found or not a file.")
        if current_branch == "main":
            found = False
            with open(base_var_path, 'r', encoding='utf-8', errors='ignore') as f_base:
                for line in f_base:
                    m = re.search(r'^\s*bbset\s+chipfinish\.source\s+\{?([^}\s]+)\}?', line, re.IGNORECASE)
                    if m: 
                        # chipfinish.source in base var is usually an absolute path or relative to where bob is run (RepoArea/run)
                        # For prepare_iter_var_file, it's safer if it's absolute or resolvable from eco_work_dir_abs's parent
                        base_chipfinish_src = m.group(1).strip()
                        if not os.path.isabs(base_chipfinish_src):
                             # Assuming it's relative to the parent of eco_work_dir_abs (i.e., RepoArea/run)
                             base_chipfinish_src = os.path.abspath(os.path.join(os.path.dirname(eco_work_dir_abs), base_chipfinish_src))
                        chipfinish_source_dir = base_chipfinish_src
                        chipfp_dir = os.path.join(chipfinish_source_dir, "pnr", "chipfinish")
                        found=True; break
            if not found: raise ValueError(f"chipfinish.source not in base var: {base_var_path}")
        else:
            if not prev_branch: raise ValueError("Prev branch needed for non-main iter.")
            # prev_branch is a directory inside eco_work_dir_abs
            chipfinish_source_dir = os.path.join(eco_work_dir_abs, prev_branch) 
            chipfp_dir = os.path.join(chipfinish_source_dir, "pnr", "chipfinish")
        if status_updater: status_updater(f"DEBUG: chipfinish.source: {chipfinish_source_dir}, chipfp_dir: {chipfp_dir}")
    except Exception as e: 
        if status_updater: status_updater(f"ERROR: Chipfinish dir path error: {e}")
        return None

    # pre_dir_context is <eco_work_dir_abs>/<current_branch>
    pre_dir_context = os.path.join(eco_work_dir_abs, current_branch) 
    try:
        if not chipfinish_source_dir: raise ValueError("chipfinish_source_dir not determined.")
        bbsets = f"""\n# --- Auto-gen settings by ECO script for branch: {current_branch} ---
bbset chipfinish.source {{{chipfinish_source_dir}}}
bbset pex.FillOasisFiles {{{os.path.join(pre_dir_context, 'pdp/dummyfill/outs', f'{block_name}.dummyfill.beol.oas')}}}
bbset pex.source {{{os.path.join(pre_dir_context, 'pex')}}}
bbset pnr.applyeco.InputDatabase {{{os.path.join(chipfp_dir, 'outs', f'{block_name}.{op}')}}}
bbset pnr.applyeco.ECOChangeFile {{{os.path.join(pre_dir_context, 'pceco/pceco/outs', f'eco.{t}.tcl')}}}
bbset pnr.applyeco.ECOPrefix "MECO_{current_branch}_"
bbset fc.ApplyEcoIncrementalUPF {{{os.path.join(pre_dir_context, 'pceco/pceco/outs', f'{block_name}.incr.upf')}}}
# --- End Auto-gen settings ---\n"""
    except Exception as e: 
        if status_updater: status_updater(f"ERROR: Gen bbset cmds error: {e}")
        return None

    temp_var_file_path = None
    try:
        temp_dir_for_vars = os.path.dirname(base_var_path) or os.path.abspath(eco_work_dir_abs) # Place near base or in eco_work_dir
        if not os.path.isdir(temp_dir_for_vars) or not os.access(temp_dir_for_vars, os.W_OK) :
             temp_dir_for_vars = tempfile.gettempdir() 
             if status_updater: status_updater(f"DEBUG: Using system temp dir for appended var file: {temp_dir_for_vars}")

        fd, temp_var_file_path = tempfile.mkstemp(suffix=".var", prefix=f"{current_branch}_appended_", text=True, dir=temp_dir_for_vars)
        with os.fdopen(fd, 'w', encoding='utf-8', errors='ignore') as temp_f:
            with open(base_var_path, 'r', encoding='utf-8', errors='ignore') as f_base: shutil.copyfileobj(f_base, temp_f)
            temp_f.write(bbsets)
        if status_updater: status_updater(f"DEBUG: Created temp appended var: {temp_var_file_path} (from base: {base_var_path})")
        return temp_var_file_path
    except Exception as e:
        if status_updater: status_updater(f"ERROR: Creating/writing temp appended var file: {e}")
        if temp_var_file_path and os.path.exists(temp_var_file_path):
            try: os.remove(temp_var_file_path)
            except OSError: 
                if status_updater: status_updater(f"WARN: Failed to remove temp var {temp_var_file_path} on prepare error.")
        return None

#******kumkumn******
def _add_analysis_bbsets_to_var_file(temp_var_file_path, current_iter_name,
                                     analysis_sequences_name, status_updater=None):
    """
    Appends specific pceco.EcoOrder and pceco.SMSA1 bbset commands
    to the given temporary variable file, based on the current iteration.
    returns True if the bbsets were successfully added or if no bbsets were required        
    """
    is_last_iter_run = (current_iter_name == "Last_iter")
    specific_iteration_bbsets = ""

    if not is_last_iter_run:
        analysis_index = -1

        if current_iter_name == "main":
            analysis_index = 0
        elif current_iter_name.startswith("Iter_"):
            try:
                iter_num = int(current_iter_name.split('_')[1])
                analysis_index = iter_num
            except ValueError:
                if status_updater:
                    status_updater(f"WARN: Could not parse iteration number from '{current_iter_name}'. Skipping specific analysis bbsets.")
                return True # Not a fatal error, just skip adding specific bbsets

        if 0 <= analysis_index < len(analysis_sequences_name):
            current_iteration_analysis = analysis_sequences_name[analysis_index]
            specific_iteration_bbsets = f"""
bbset pceco.EcoOrder {{SMSA1}}
bbset pceco.SMSA1 {current_iteration_analysis}
"""
            if status_updater:
                status_updater(f"DEBUG: Preparing to append specific analysis bbsets for {current_iter_name}: {specific_iteration_bbsets.strip()}")
        elif analysis_sequences_name:
            if status_updater:
                status_updater(f"WARN: Analysis sequence index {analysis_index} out of bounds for {current_iter_name}. Using default flow.")
        else:
            if status_updater:
                status_updater(f"INFO: No analysis sequences provided. Using default flow for {current_iter_name}.")
    else:
        if status_updater:
            status_updater(f"INFO: Skipping specific analysis bbsets for 'Last_iter' as per requirement.")
        return True # Nothing to add for Last_iter, so consider it successful

    # Only attempt to write if there's something to write
    if specific_iteration_bbsets:
        try:
            with open(temp_var_file_path, 'a', encoding='utf-8', errors='ignore') as f_temp_var:
                f_temp_var.write(specific_iteration_bbsets)
            if status_updater:
                status_updater(f"INFO: Appended specific analysis bbsets for '{current_iter_name}' to {temp_var_file_path}")
            return True
        except Exception as e:
            if status_updater:
                status_updater(f"ERROR: Failed to append specific analysis bbsets to {temp_var_file_path}: {e}")
            return False
    return True # If specific_iteration_bbsets was empty (e.g., due to parsing warning), still success.



# run_eco_logic's `eco_work_dir` parameter is the absolute path to the specific ECO run directory
def run_eco_logic(initial_base_var_file, block_name, tool_name, analysis_sequences_name, num_iterations, eco_work_dir, # eco_work_dir is already absolute
                  timeout_minutes, check_interval,
                  status_updater, summary_updater, completion_callback, get_gui_base_var_file_func):
    global failed_info
    # eco_work_dir is already the absolute path, e.g., /path/to/RepoArea/run/MyEcoRun1
    # No need for os.path.abspath(eco_work_dir) here again.
    current_stage_for_error, current_branch_for_error, process_outcome = "setup", "N/A", "UNKNOWN"

    current_effective_base_for_iterations = initial_base_var_file
    status_updater(f"INFO: Initial effective base var for iterations: {current_effective_base_for_iterations}")

    try:
        status_updater(f"INFO: Starting ECO: BLOCK {block_name}, TOOL {tool_name}, BaseVar {initial_base_var_file}, ECO Work Dir: {eco_work_dir}")
        summary_updater("Initializing...")
        failed_info.update({"is_failed_state": False})
        if not os.path.exists(eco_work_dir): # Check and create the specific ECO work directory
            os.makedirs(eco_work_dir)
            status_updater(f"INFO: Created ECO work directory: {eco_work_dir}")

        all_iterations = ["main"] + [f"Iter_{i}" for i in range(1, num_iterations)] + (["Last_iter"] if num_iterations >=1 else [])
        prev_iter = "" # Relative to eco_work_dir

        for i, current_iter_name in enumerate(all_iterations): # current_iter_name is "main", "Iter_1" etc.
            current_branch_for_error = current_iter_name
            is_last_iter_run = (current_iter_name == "Last_iter")
            stages_to_run_for_current_iter = LAST_ITER_STAGES if is_last_iter_run else MAIN_ITER_STAGES

            status_updater(f"\n{'='*20} Prep Branch: '{current_iter_name}' (Prev: {prev_iter}) in {eco_work_dir} {'='*20}")
            summary_updater(f"Preparing Branch: {current_iter_name}")
            if abort_flag.is_set(): raise RuntimeError("Aborted before branch prep.")

            current_stage_for_error = f"prepare_var_{current_iter_name}"
            # prepare_iter_var_file needs eco_work_dir to construct paths for bbset commands
            temp_appended_var_for_branch_creation = prepare_iter_var_file(
                current_effective_base_for_iterations, 
                current_iter_name, prev_iter, eco_work_dir, block_name, tool_name, status_updater
            )
            if not temp_appended_var_for_branch_creation:
                raise RuntimeError(f"FAIL: Prepare var for '{current_iter_name}' using base '{current_effective_base_for_iterations}'.")
            #******kumkumn******
            #added the new helper function here to add bbset commands
            if not _add_analysis_bbsets_to_var_file(
                temp_appended_var_for_branch_creation,
                current_iter_name,
                analysis_sequences_name,
                status_updater
            ):
                raise RuntimeError(f"FAIL: Failed to add specific analysis bbsets for '{current_iter_name}'.")

            current_stage_for_error = f"create_{current_iter_name}"
            # bob create -r <eco_work_dir> ...
            create_args = ["-r", eco_work_dir, "-v", temp_appended_var_for_branch_creation, "-s"] + stages_to_run_for_current_iter
            if current_iter_name != "main": create_args = ["--branch", current_iter_name] + create_args
            create_cmd = ["bob", "create"] + create_args

            # run_bob_command's work_dir should be where bob commands are effective, typically the parent of eco_work_dir (RepoArea/run)
            # or can be eco_work_dir itself if bob handles -r correctly for all sub-commands from there.
            # For `bob create -r <abs_eco_work_dir>`, cwd can be anything, but for consistency, let's use parent of eco_work_dir.
            # This assumes CWD was set to RepoArea/run before run_eco_logic was called.
            # Let's pass eco_work_dir as the work_dir for run_bob_command, as -r specifies the root.
            if not run_bob_command(create_cmd, work_dir=eco_work_dir, status_updater=status_updater, summary_updater=summary_updater, timeout_minutes=timeout_minutes):
                if os.path.exists(temp_appended_var_for_branch_creation):
                    try: os.remove(temp_appended_var_for_branch_creation)
                    except OSError: 
                        if status_updater: status_updater(f"WARN: Failed to remove {temp_appended_var_for_branch_creation} after failed create.")
                raise RuntimeError(f"FAIL: 'bob create' for '{current_iter_name}'.")
            
            if os.path.exists(temp_appended_var_for_branch_creation): 
                try: os.remove(temp_appended_var_for_branch_creation)
                except OSError as e: 
                    if status_updater: status_updater(f"WARN: Del temp var {temp_appended_var_for_branch_creation} fail: {e}")
            temp_appended_var_for_branch_creation = None 

            status_updater(f"INFO: --- Running stages for '{current_iter_name}' ---")
            current_stage_index_in_branch = 0 
            while current_stage_index_in_branch < len(stages_to_run_for_current_iter):
                stage_type = stages_to_run_for_current_iter[current_stage_index_in_branch] 
                stage_node_pattern_for_run = f"{stage_type}/*" 
                current_stage_for_error = f"run_{current_iter_name}_{stage_type}"
                
                if abort_flag.is_set(): raise RuntimeError(f"Aborted before stage {stage_type}.")
                status_updater(f"INFO: --- Starting stage type: {stage_type} (using pattern: {stage_node_pattern_for_run}) ---")

                run_cmd = ["bob", "run", "-r", eco_work_dir, "--branch", current_iter_name, "--node", stage_node_pattern_for_run]
                if not run_bob_command(run_cmd, work_dir=eco_work_dir, status_updater=status_updater, summary_updater=summary_updater, timeout_minutes=timeout_minutes):
                    failed_info.update({"is_failed_state":True, "branch":current_iter_name, "node_pattern":stage_node_pattern_for_run, "specific_node":f"{stage_node_pattern_for_run} (bob run cmd fail)"})
                    process_outcome="FAILED"; completion_callback(process_outcome)
                    status_updater(f"ERROR: 'bob run' for {stage_node_pattern_for_run} fail. User action needed."); summary_updater(f"Br:{current_iter_name},St:{stage_type}, 'bob run' FAIL. Click Continue.")
                    continue_event.wait(); continue_event.clear()
                    if abort_flag.is_set(): raise RuntimeError("Aborted after 'bob run' cmd fail.")
                    status_updater("INFO: Continue. Retry 'bob run'..."); summary_updater(f"Br:{current_iter_name},St:{stage_type}, Retry 'bob run'"); failed_info["is_failed_state"]=False; continue

                status_updater(f"INFO: --- Waiting for stage type: {stage_type} (pattern: {stage_node_pattern_for_run}) ---")
                wait_status, wait_msg, specific_failed_node_from_json = wait_for_bob_run(eco_work_dir, current_iter_name, stage_node_pattern_for_run, timeout_minutes, check_interval, status_updater, summary_updater)
                
                #******kumkumn******
                #also continue if wait_for_bob_run returns SKIPPED_WAIT
                if wait_status == "VALID": 
                    status_updater(f"INFO: --- Stage type {stage_type} COMPLETED ---")
                    current_stage_index_in_branch += 1
                    continue
                elif wait_status == "FAILED":
                    latest_actual_failed_node_path = specific_failed_node_from_json 
                    
                    while True: 
                        failed_info.update({
                            "is_failed_state": True, 
                            "branch": current_iter_name, 
                            "node_pattern": stage_node_pattern_for_run, 
                            "specific_node": latest_actual_failed_node_path 
                        })
                        process_outcome="FAILED"; completion_callback(process_outcome)
                        status_updater(f"ERROR: Stage type {stage_type} fail @ node '{latest_actual_failed_node_path}'. User action (Continue/Abort).")
                        continue_event.wait(); continue_event.clear()
                        if abort_flag.is_set(): raise RuntimeError(f"Aborted waiting to continue after fail @ {latest_actual_failed_node_path}.")
                        
                        gui_base_var_path_on_continue = get_gui_base_var_file_func()
                        #******kumkumn******
                        #on pressing continue, tool should again append the default and optional var files
                        
                        if not os.path.isfile(gui_base_var_path_on_continue):
                            status_updater(f"ERROR: New Base Var '{gui_base_var_path_on_continue}' invalid. Fix & Continue.")
                            summary_updater(f"Br:{current_iter_name},St:{stage_type}, Invalid new Base Var. Fix & Continue."); continue

                        if gui_base_var_path_on_continue != current_effective_base_for_iterations:
                            status_updater(f"INFO: Base Var changed by user from '{current_effective_base_for_iterations}' to '{gui_base_var_path_on_continue}'.")
                            summary_updater(f"Br:{current_iter_name},St:{stage_type}, BaseVar changed, updating flow.vars...")
                            
                            newly_appended_var_for_flow_override = prepare_iter_var_file(
                                gui_base_var_path_on_continue, current_iter_name, prev_iter, 
                                eco_work_dir, block_name, tool_name, status_updater
                            )
                            if not newly_appended_var_for_flow_override:
                                status_updater(f"ERROR: Re-prep var file with new base FAIL. User action needed."); summary_updater(f"Br:{current_iter_name},St:{stage_type}, Re-prep var FAIL."); continue
                            
                            #******kumkumn******
                            #also add bbsets to the new var file
                            if not _add_analysis_bbsets_to_var_file(
                                newly_appended_var_for_flow_override,
                                current_iter_name,
                                analysis_sequences_name,
                                status_updater
                            ):
                            # If adding the analysis bbsets fails
                                status_updater(f"ERROR: Failed to add specific analysis bbsets for flow override for '{current_iter_name}'. Aborting.");
                                summary_updater(f"Br:{current_iter_name},St:{stage_type}, Add Analysis BBSET FAIL.");
                                continue # Or raise a RuntimeError, depending on desired error behavior
                            #code segment to add bbset commands end here 

                            status_updater(f"INFO: Updating flow.var for relevant stages/nodes in branch '{current_iter_name}' using content from '{newly_appended_var_for_flow_override}'.")
                            
                            for stage_update_idx in range(current_stage_index_in_branch, len(stages_to_run_for_current_iter)):
                                stage_dir_name_to_update = stages_to_run_for_current_iter[stage_update_idx] 
                                current_stage_dir_path = os.path.join(eco_work_dir, current_iter_name, stage_dir_name_to_update)

                                if not os.path.isdir(current_stage_dir_path):
                                    status_updater(f"WARN: Stage directory {current_stage_dir_path} not found. Skipping flow.var update for it.")
                                    continue
                                
                                node_dirs_in_stage = []
                                if stage_dir_name_to_update == "pex":
                                    node_dirs_in_stage.append("pex.starrc") 
                                else:
                                    try: 
                                        node_dirs_in_stage = [d for d in os.listdir(current_stage_dir_path) if os.path.isdir(os.path.join(current_stage_dir_path, d))]
                                    except OSError as e_ls:
                                        status_updater(f"WARN: Could not list nodes in {current_stage_dir_path}: {e_ls}. Skipping for this stage.")
                                        continue
                                
                                for k_node_dir_name in node_dirs_in_stage: 
                                    full_k_node_path_in_branch = os.path.join(current_stage_dir_path, k_node_dir_name)
                                    target_flow_var_path = os.path.join(full_k_node_path_in_branch, "vars", "flow.var")

                                    if os.path.isfile(target_flow_var_path):
                                        backup_flow_var_path = os.path.join(full_k_node_path_in_branch, "vars", "flow.var_old")
                                        try:
                                            shutil.move(target_flow_var_path, backup_flow_var_path)
                                            status_updater(f"INFO: Backed up '{target_flow_var_path}' to '{backup_flow_var_path}'.")
                                            shutil.copy(newly_appended_var_for_flow_override, target_flow_var_path)
                                            status_updater(f"INFO: Copied new var content to '{target_flow_var_path}'.")
                                        except Exception as e_file_copy:
                                            status_updater(f"ERROR: Updating '{target_flow_var_path}' failed: {e_file_copy}")
                                    else:
                                        status_updater(f"INFO: Skipping non-existent flow.var: {target_flow_var_path}")
                            
                            if os.path.exists(newly_appended_var_for_flow_override): 
                                try: 
                                    os.remove(newly_appended_var_for_flow_override)
                                    status_updater(f"DEBUG: Removed temp appended var used for flow.var override: {newly_appended_var_for_flow_override}")
                                except OSError as e_rem: 
                                    status_updater(f"WARN: Del temp {newly_appended_var_for_flow_override} fail: {e_rem}")
                            
                            current_effective_base_for_iterations = gui_base_var_path_on_continue
                            status_updater(f"INFO: Base var for subsequent iterations updated to: '{current_effective_base_for_iterations}'.")
                        else:
                            status_updater(f"INFO: Base Var File not changed by user.")

                        status_updater(f"INFO: Continue. Retrying failed node '{latest_actual_failed_node_path}'..."); summary_updater(f"Br:{current_iter_name},St:{stage_type}, Retry {latest_actual_failed_node_path}")
                        failed_info["is_failed_state"]=False
                        
                        inval_cmd_list = ["bob", "update","status", "-r", eco_work_dir,"--branch", current_iter_name, "--invalidate", latest_actual_failed_node_path,"-f"]
                        rerun_cmd_list = ["bob", "run", "-r", eco_work_dir, "--branch", current_iter_name, "--node", latest_actual_failed_node_path]
                        
                        status_updater(f"INFO: Invalidating node: {' '.join(inval_cmd_list)}")
                        if not run_bob_command(inval_cmd_list, work_dir=eco_work_dir, status_updater=status_updater, summary_updater=None, timeout_minutes=5):
                            status_updater(f"ERROR: 'bob update status' (invalidate) failed for {latest_actual_failed_node_path}. User action needed."); summary_updater(f"Br:{current_iter_name},St:{stage_type}, Invalidate FAIL."); continue
                        
                        status_updater(f"INFO: Re-running node: {' '.join(rerun_cmd_list)}")
                        if not run_bob_command(rerun_cmd_list, work_dir=eco_work_dir, status_updater=status_updater, summary_updater=summary_updater, timeout_minutes=timeout_minutes):
                            status_updater(f"ERROR: 'bob run' (retry) for {latest_actual_failed_node_path} fail. User action needed."); summary_updater(f"Br:{current_iter_name},St:{stage_type}, Retry 'bob run' FAIL."); latest_actual_failed_node_path += " (retry run fail)"; continue

                        status_updater(f"INFO: --- Waiting for retried node: {latest_actual_failed_node_path} ---")
                        retry_wait_status, _, retry_specific_fail = wait_for_bob_run(eco_work_dir, current_iter_name, latest_actual_failed_node_path, timeout_minutes, check_interval, status_updater, summary_updater)
                        
                        #******kumkumn******
                        if retry_wait_status == "VALID":
                            status_updater(f"INFO: Retry SUCCESS for '{latest_actual_failed_node_path}'. Re-check overall stage type '{stage_node_pattern_for_run}'.")
                            final_check_status, _, _ = wait_for_bob_run(eco_work_dir, current_iter_name, stage_node_pattern_for_run, timeout_minutes, check_interval, status_updater, summary_updater)
                            #******kumkumn******
                            if final_check_status == "VALID":
                                status_updater(f"INFO: --- Stage type {stage_type} COMPLETED after retry. ---")
                                current_stage_index_in_branch += 1 
                                break 
                            else: 
                                status_updater(f"ERROR: Stage type {stage_type} ({stage_node_pattern_for_run}) still not VALID (status: {final_check_status}) after {latest_actual_failed_node_path} retry. User action needed.")
                                summary_updater(f"Br:{current_iter_name},St:{stage_type}, Post-retry check FAIL.")
                                latest_actual_failed_node_path = f"{stage_node_pattern_for_run} (post-retry check fail)" 
                                continue 
                        else: 
                            status_updater(f"ERROR: Retry for '{latest_actual_failed_node_path}' FAIL (status {retry_wait_status}). User action needed.")
                            summary_updater(f"Br:{current_iter_name},St:{stage_type}, Retry {latest_actual_failed_node_path} VALIDATION FAIL.")
                            latest_actual_failed_node_path = retry_specific_fail if retry_specific_fail else latest_actual_failed_node_path + " (retry wait fail)"
                            continue 

                elif wait_status == "TIMEOUT": process_outcome="TIMEOUT"; raise RuntimeError(f"TIMEOUT waiting for {stage_type} ({stage_node_pattern_for_run}). {wait_msg}")
                elif wait_status == "ABORTED": process_outcome="ABORTED"; raise RuntimeError(f"ABORTED waiting for {stage_type} ({stage_node_pattern_for_run}). {wait_msg}")
                elif wait_status in ["ERROR", "JSON_ERROR", "FILE_ERROR"]: process_outcome="ERROR"; raise RuntimeError(f"ERROR ({wait_status}) for {stage_type}: {wait_msg}")
                else: process_outcome="ERROR"; raise RuntimeError(f"Unknown status from wait: {wait_status} for {stage_type}")
            prev_iter = current_iter_name # prev_iter is the name of the branch dir inside eco_work_dir
        process_outcome = "SUCCESS"
        status_updater(f"\n{'='*20} All iterations COMPLETED successfully. {'='*20}"); summary_updater("ECO Process Completed Successfully.")
    except Exception as e:
        if process_outcome=="UNKNOWN": process_outcome="ERROR" if not abort_flag.is_set() else "ABORTED"
        err_msg = f"\n{'!'*20} ECO PROCESS {process_outcome} {'!'*20}\n"
        err_msg += f"REASON: {e}\n" if "Process aborted" in str(e) else f"ERROR @ Br: {current_branch_for_error}, St: {current_stage_for_error}\nDETAILS: {e}\n"
        summary_updater(f"{process_outcome}: {current_stage_for_error}")
        status_updater(err_msg)
    finally:
        if not failed_info.get("is_failed_state", False): completion_callback(process_outcome)

class EcoRunnerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multi-ECO Utility (v1.06)")
        self.geometry("850x500") # Initial size, will adjust with log toggle
        self.processing_thread, self.clear_thread, self.setup_thread = None, None, None
        self.is_log_visible = False # Initial state of the detailed log
        self.current_run_var_file_path = None
        self.current_run_dir = None

        # --- Styling ---
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

        # --- Configuration Frame ---
        config_frame = ttk.LabelFrame(self, text="Configuration", padding="15"); config_frame.pack(fill=tk.X, padx=10, pady=5); current_row = 0

        # Load/Save Config Buttons
        config_io_frame = ttk.Frame(config_frame); config_io_frame.grid(row=current_row, column=0, columnspan=4, pady=(0,10), sticky=tk.W)
        self.load_config_button = ttk.Button(config_io_frame, text="Load Config", command=self.load_configuration, style="File.TButton"); self.load_config_button.pack(side=tk.LEFT, padx=(0,5))
        self.save_config_button = ttk.Button(config_io_frame, text="Save Config", command=self.save_configuration, style="File.TButton"); self.save_config_button.pack(side=tk.LEFT, padx=5); current_row += 1
        
        # Repo Area
        ttk.Label(config_frame, text="Repo Area (for setup):", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.repo_area_var = tk.StringVar(value=DEFAULT_SETUP_REPO_AREA)
        self.repo_area_entry = ttk.Entry(config_frame, textvariable=self.repo_area_var, width=60)
        self.repo_area_entry.grid(row=current_row, column=1, columnspan=2, padx=5, pady=3, sticky=tk.EW)
        self.repo_area_browse = ttk.Button(config_frame, text="Browse...", command=lambda: self.browse_directory(self.repo_area_var), style="File.TButton"); self.repo_area_browse.grid(row=current_row, column=3, padx=5, pady=3); current_row+=1
        
        # IP Name
        ttk.Label(config_frame, text="IP Name:", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.ip_var = tk.StringVar(value=DEFAULT_IP_NAME); self.ip_entry = ttk.Entry(config_frame, textvariable=self.ip_var, width=30); self.ip_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W); current_row+=1
        
        # Chip Name
        ttk.Label(config_frame, text="Chip Name:", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.chip_var = tk.StringVar(value=DEFAULT_CHIP_NAME); self.chip_entry = ttk.Entry(config_frame, textvariable=self.chip_var, width=30); self.chip_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W); current_row+=1
        
        # Process
        ttk.Label(config_frame, text="Process:", foreground="darkgreen").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.process_var = tk.StringVar(value=DEFAULT_PROCESS_NODE); self.process_entry = ttk.Entry(config_frame, textvariable=self.process_var, width=30); self.process_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W); current_row+=1
        
        # ECO Work Dir Name
        ttk.Label(config_frame, text="ECO Work Dir Name :", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.eco_work_dir_name_var = tk.StringVar(value=DEFAULT_ECO_WORK_DIR_NAME)
        self.eco_work_dir_name_entry = ttk.Entry(config_frame, textvariable=self.eco_work_dir_name_var, width=60)
        self.eco_work_dir_name_entry.grid(row=current_row, column=1, columnspan=2, padx=5, pady=3, sticky=tk.EW)
        current_row+=1

        # Block Name
        ttk.Label(config_frame, text="BLOCK Name:", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.block_var = tk.StringVar(); self.block_entry = ttk.Entry(config_frame, textvariable=self.block_var, width=30); self.block_entry.grid(row=current_row, column=1, padx=5, pady=3, sticky=tk.W)
        
        # Tool Selection
        ttk.Label(config_frame, text="TOOL:", foreground="darkblue").grid(row=current_row, column=2, padx=(20,5), pady=3, sticky=tk.W)
        self.tool_var = tk.StringVar(); self.tool_combo = ttk.Combobox(config_frame, textvariable=self.tool_var, values=["FC", "Innovus"], state="readonly", width=10); self.tool_combo.grid(row=current_row, column=3, padx=5, pady=3, sticky=tk.W+tk.E); self.tool_combo.set("FC"); current_row+=1
        
        # Base Var File
        ttk.Label(config_frame, text="Base Var File (optional):", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.var_file_var = tk.StringVar(); self.var_file_entry = ttk.Entry(config_frame, textvariable=self.var_file_var, width=60); self.var_file_entry.grid(row=current_row, column=1, columnspan=2, padx=5, pady=3, sticky=tk.EW)
        self.var_file_browse = ttk.Button(config_frame, text="Browse...", command=lambda: self.browse_file(self.var_file_var), style="File.TButton"); self.var_file_browse.grid(row=current_row, column=3, padx=5, pady=3); current_row+=1
        
        # Analysis per Iteration (main input for iterations)
        ttk.Label(config_frame, text="Analysis per Iteration :", foreground="darkblue").grid(row=current_row, column=0, padx=5, pady=3, sticky=tk.W)
        self.analysis_input_text = tk.Text(config_frame, width=50, height=3, wrap=tk.WORD)
        self.analysis_input_text.grid(row=current_row, column=1, columnspan=2, padx=5, pady=3, sticky=tk.EW)
        self.analysis_input_text.insert(tk.END, DEFAULT_ANALYSIS_INPUT)
        current_row+=1

        # BOB Timeout and Check Interval
        timeout_frame = ttk.Frame(config_frame); timeout_frame.grid(row=current_row, column=0, columnspan=4, sticky=tk.W, padx=5, pady=3)
        ttk.Label(timeout_frame, text="BOB Timeout (min):").pack(side=tk.LEFT, padx=(0,5)); self.timeout_var = tk.StringVar(value=str(DEFAULT_BOB_RUN_TIMEOUT_MINUTES)); self.timeout_entry = ttk.Entry(timeout_frame, textvariable=self.timeout_var, width=8); self.timeout_entry.pack(side=tk.LEFT, padx=(0,15))
        ttk.Label(timeout_frame, text="Check Interval (sec):").pack(side=tk.LEFT, padx=(0,5)); self.interval_var = tk.StringVar(value=str(DEFAULT_BOB_CHECK_INTERVAL_SECONDS)); self.interval_entry = ttk.Entry(timeout_frame, textvariable=self.interval_var, width=8); self.interval_entry.pack(side=tk.LEFT)
        current_row+=1; config_frame.columnconfigure(1, weight=1)

        control_frame = ttk.Frame(self, padding="10"); control_frame.pack(fill=tk.X, pady=5)
        self.start_button = ttk.Button(control_frame, text="START ECO", command=self.start_processing, style="Start.TButton"); self.start_button.pack(side=tk.LEFT, padx=5)
        self.continue_button = ttk.Button(control_frame, text="CONTINUE", command=self.continue_processing, style="Continue.TButton", state=tk.DISABLED); self.continue_button.pack(side=tk.LEFT, padx=5)
        self.abort_button = ttk.Button(control_frame, text="ABORT", command=self.abort_processing, style="Abort.TButton", state=tk.DISABLED); self.abort_button.pack(side=tk.LEFT, padx=5)
        self.clear_button = ttk.Button(control_frame, text="Clear ECO Work Dir", command=self.clear_runs, style="Clear.TButton"); self.clear_button.pack(side=tk.LEFT, padx=5)
        self.quit_button = ttk.Button(control_frame, text="Quit", command=self.quit_app, style="Quit.TButton"); self.quit_button.pack(side=tk.RIGHT, padx=10)

        summary_frame = ttk.Frame(self, padding=(10,5,10,5)); summary_frame.pack(fill=tk.X)
        ttk.Label(summary_frame, text="Status:", font=('TkDefaultFont', 10, 'bold')).pack(side=tk.LEFT)
        self.summary_var = tk.StringVar(value="Idle."); self.summary_label = ttk.Label(summary_frame, textvariable=self.summary_var, font=('TkDefaultFont',10), anchor=tk.W); self.summary_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        log_control_frame = ttk.Frame(self, padding=(10,0,10,5)); log_control_frame.pack(fill=tk.X)
        self.log_toggle_var = tk.BooleanVar(value=False); self.log_toggle_check = ttk.Checkbutton(log_control_frame, text="Show Detailed Log", variable=self.log_toggle_var, command=self.toggle_log_display, style="Link.TCheckbutton"); self.log_toggle_check.pack(side=tk.LEFT)
        self.status_frame = ttk.LabelFrame(self, text="Status Log", padding="10"); self.status_text = scrolledtext.ScrolledText(self.status_frame, wrap=tk.WORD, height=15, state=tk.DISABLED, font=("Courier New",9)); self.status_text.pack(fill=tk.BOTH, expand=True); self.toggle_log_display()
        watermark_label = ttk.Label(self, text="MECO Runner v1.06 - askakshay@google.com", foreground="gray50", font=("TkDefaultFont",7)); watermark_label.place(relx=1.0, rely=1.0, anchor=tk.SE, x=-5, y=-5)
        self.update_status_display("INFO: Welcome! Ensure 'bob' is in PATH. Config Repo Area and run parameters.")

    def get_timestamp(self): return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def update_status_display(self, message):
        if not hasattr(self,'status_text') or not self.status_text.winfo_exists(): return
        log_msg = f"{self.get_timestamp()} - {message}\n"
        def _update():
            try:
                self.status_text.config(state=tk.NORMAL)
                self.status_text.insert(tk.END, log_msg)
                if self.is_log_visible: 
                    self.status_text.see(tk.END)
                self.status_text.config(state=tk.DISABLED) 
            except tk.TclError: 
                pass 
            except Exception as e: 
                print(f"GUI Error (status display update): {e}")
        self.after(0, _update)

    def update_summary_display(self, message):
        if not hasattr(self,'summary_var') or not self.winfo_exists(): return
        disp_msg = message[:120] + '...' if len(message)>120 else message
        def _update():
            try: self.summary_var.set(disp_msg)
            except tk.TclError: pass 
            except Exception as e: print(f"GUI Error (summary): {e}")
        self.after(0, _update)
    def toggle_log_display(self):
        if self.log_toggle_var.get():
            if not self.is_log_visible: self.status_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0,25)); self.is_log_visible=True; self.geometry(""); self.status_text.see(tk.END)
        else:
            if self.is_log_visible: self.status_frame.pack_forget(); self.is_log_visible=False; self.geometry("")
    def browse_file(self, var_to_set):
        fname = filedialog.askopenfilename(title="Select File",filetypes=(("Var/TCL","*.var *.tcl"),("All","*.*")))
        if fname: var_to_set.set(fname)
    def browse_directory(self, var_to_set):
        initial_dir_val = var_to_set.get()
        if not os.path.isdir(initial_dir_val): initial_dir_val = "."
        dname = filedialog.askdirectory(title="Select Directory",initialdir=initial_dir_val)
        if dname: var_to_set.set(dname)

    def set_controls_state(self, mode, outcome="UNKNOWN"):
        is_running, is_failed, is_idle = mode in ['running','setup'], mode == 'failed_wait', mode in ['idle','finished']
        idle_state = tk.NORMAL if is_idle else tk.DISABLED
        self.start_button.config(state=idle_state); self.clear_button.config(state=idle_state)
        self.load_config_button.config(state=idle_state); self.save_config_button.config(state=idle_state)
        self.abort_button.config(state=tk.NORMAL if is_running or is_failed else tk.DISABLED)
        self.continue_button.config(state=tk.NORMAL if is_failed else tk.DISABLED)
        
        input_state_general = tk.NORMAL if is_idle else tk.DISABLED
        readonly_state_combo = "readonly" if is_idle else tk.DISABLED 
        base_var_input_state_special = tk.NORMAL if (is_idle or is_failed) else tk.DISABLED
        
        #******kumkumn******
        #added analysis_input_text
        for entry_widget, browse_button_widget, state_category_key in [
            (self.repo_area_entry, self.repo_area_browse, "general"), # Renamed
            (self.ip_entry, None, "general"), (self.chip_entry, None, "general"), (self.process_entry, None, "general"),
            (self.eco_work_dir_name_entry, None, "general"), # Changed to eco_work_dir_name_entry, browse removed for now
            (self.block_entry, None, "general"),
            (self.var_file_entry, self.var_file_browse, "base_var_special"), 
            (self.analysis_input_text, None, "general"), (self.timeout_entry, None, "general"), (self.interval_entry, None, "general")
        ]:
            current_actual_state = base_var_input_state_special if state_category_key == "base_var_special" else input_state_general
            entry_widget.config(state=current_actual_state)
            if browse_button_widget: browse_button_widget.config(state=tk.NORMAL if current_actual_state == tk.NORMAL else tk.DISABLED)
        
        self.tool_combo.config(state=readonly_state_combo)


    def processing_complete(self, outcome):
        global failed_info; self.processing_thread = None
        if not self.winfo_exists(): return
        self.update_status_display(f"PROCESS: ECO Run Outcome = {outcome}")
        final_msg_map = { "SUCCESS": ("ECO Run Complete", "The ECO process finished successfully.", messagebox.showinfo),
            "FAILED": ("Run Failed", f"Run failed @ node: {failed_info.get('specific_node','N/A')}\nFix issue, optionally edit Base Var File, and click CONTINUE, or ABORT.", messagebox.showwarning),
            "ABORTED": ("ECO Run Aborted", "The ECO process was aborted.", messagebox.showwarning),
            "TIMEOUT": ("ECO Run Timeout", "The ECO process timed out.", messagebox.showerror), }
        title, msg, mbox_func = final_msg_map.get(outcome, ("ECO Run Error", f"ECO process failed: {outcome}.", messagebox.showerror))
        mbox_func(title, msg)
        final_state = 'failed_wait' if outcome == "FAILED" else 'finished'
        self.set_controls_state(final_state, outcome)
        if outcome != "FAILED": abort_flag.clear(); failed_info['is_failed_state'] = False

    def clear_complete(self):
        self.clear_thread = None;
        if not self.winfo_exists(): return
        self.update_status_display("PROCESS: Clear ECO Work Dir finished."); self.update_summary_display("Clear ECO Work Dir Finished.")
        self.set_controls_state('idle')
    def clear_runs(self):
        repo_area = self.repo_area_var.get().strip()
        eco_work_dir_name = self.eco_work_dir_name_var.get().strip()
        if not repo_area or not eco_work_dir_name: 
            messagebox.showerror("Input Error","Specify Repo Area and ECO Work Dir Name to clear."); return
        
        # Construct the full path to the ECO work directory to be cleared
        eco_work_dir_to_clear_abs = os.path.join(os.path.abspath(repo_area), "run", eco_work_dir_name)

        if not os.path.isdir(eco_work_dir_to_clear_abs):
            messagebox.showinfo("Not Found", f"ECO Work Dir not found, nothing to clear:\n{eco_work_dir_to_clear_abs}")
            return

        if messagebox.askyesno("Confirm Clear ECO Work Dir", f"Delete ALL contents of ECO Work Dir:\n{eco_work_dir_to_clear_abs}\n(Incl. branches: main, Iter_X, Last_iter)\n\nThis CANNOT be undone!"):
            self.update_status_display(f"PROCESS: Clearing ECO Work Dir: {eco_work_dir_to_clear_abs}"); self.update_summary_display("Clearing ECO Work Dir...")
            self.set_controls_state('running')
            self.clear_thread = threading.Thread(target=run_clear_logic, args=(eco_work_dir_to_clear_abs, self.update_status_display, self.clear_complete), daemon=True); self.clear_thread.start()
        else: self.update_status_display("INFO: Clear operation cancelled.")

    def continue_processing(self):
        global failed_info
        if failed_info.get("is_failed_state"):
            self.update_status_display("PROCESS: Continue pressed. Resuming run..."); self.update_summary_display("Continue requested...")
            self.set_controls_state('running'); continue_event.set()
        else: self.update_status_display("WARN: Continue pressed but not in a failed state.")

    def abort_processing(self):
        self.update_status_display("PROCESS: Abort pressed. Requesting stop..."); self.update_summary_display("Abort requested... 'bob stop'...")
        self.abort_button.config(state=tk.DISABLED); self.continue_button.config(state=tk.DISABLED)
        
        repo_area = self.repo_area_var.get().strip()
        eco_work_dir_name = self.eco_work_dir_name_var.get().strip()

        if repo_area and eco_work_dir_name:
            eco_work_dir_to_stop_abs = os.path.join(os.path.abspath(repo_area), "run", eco_work_dir_name)
            if os.path.isdir(eco_work_dir_to_stop_abs): # Only run stop if dir exists
                stop_cmd = ["bob", "stop", "-r", eco_work_dir_to_stop_abs]
                self.update_status_display(f"RUNNING: {' '.join(stop_cmd)}")
                try:
                    # CWD for bob stop should be where it can find .bobmeta, usually eco_work_dir_to_stop_abs or its parent
                    stop_res = subprocess.run(stop_cmd, cwd=os.path.dirname(eco_work_dir_to_stop_abs), # Try running from RepoArea/run
                                            shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                            encoding='utf-8', errors='ignore', timeout=30)
                    if stop_res.returncode==0: self.update_status_display(f"INFO: 'bob stop -r {eco_work_dir_to_stop_abs}' completed.")
                    else: self.update_status_display(f"WARN: 'bob stop -r {eco_work_dir_to_stop_abs}' failed (code {stop_res.returncode}). Stderr: {stop_res.stderr.strip()}")
                except Exception as e: self.update_status_display(f"ERROR running 'bob stop': {e}")
            else:
                self.update_status_display(f"WARN: ECO Work Dir '{eco_work_dir_to_stop_abs}' not found, cannot run 'bob stop'.")
        else: self.update_status_display("WARN: Repo Area or ECO Work Dir Name not specified for 'bob stop'.")
        abort_flag.set(); continue_event.set()
    
    #******kumkumn******
    def _prepare_run_var_file(self):
        """
        Prepares the 'var_file' for the current run based on user input.
        The generated run_var_file.var will be placed in the script's directory.

        Returns:
            str: The absolute path to the prepared var_file for the current run,
                 or None if an error occurs.
        """
        script_dir = get_script_directory()
        default_var_file_path = os.path.join(script_dir, DEFAULT_BASE_VAR_FILE_NAME)
        user_var_file_input = self.var_file_var.get().strip()

        # The run var file will always be in the script directory
        self.current_run_var_file_path = os.path.join(script_dir, DEFAULT_RUN_VAR_FILE_NAME)

        try:
            # Case 1: User input field is empty
            if not user_var_file_input:
                if not os.path.exists(default_var_file_path):
                    self.update_status_display(f"ERROR: Default var file '{DEFAULT_BASE_VAR_FILE_NAME}' not found in script directory: {script_dir}")
                    return None
                # Copy the default var file to the designated run var file path
                shutil.copy(default_var_file_path, self.current_run_var_file_path)
                self.update_status_display(f"INFO: Using default var file. Created: {self.current_run_var_file_path}")
                return self.current_run_var_file_path
            # Case 2: User provided a file
            else:
                user_provided_file_path = user_var_file_input
                if not os.path.exists(user_provided_file_path):
                    self.update_status_display(f"ERROR: User provided var file '{user_provided_file_path}' not found.")
                    return None

                # Read default file content
                default_content = ""
                if os.path.exists(default_var_file_path):
                    with open(default_var_file_path, 'r') as f:
                        default_content = f.read()
                else:
                    self.update_status_display(f"WARNING: Default var file '{DEFAULT_BASE_VAR_FILE_NAME}' not found in script directory. Only using user-provided file content.")

                # Read user provided file content
                user_content = ""
                with open(user_provided_file_path, 'r') as f:
                    user_content = f.read()

                # Combine and write to the new run-specific var file
                combined_content = default_content + "\n" + user_content # Add a newline for separation
                with open(self.current_run_var_file_path, 'w') as f:
                    f.write(combined_content)

                self.update_status_display(f"INFO: Created combined var file: {self.current_run_var_file_path}")
                return self.current_run_var_file_path

        except Exception as e:
            self.update_status_display(f"ERROR preparing var file: {e}")
            return None

    #******kumkumn******
    def parse_analysis_input(self, analysis_input_string):

        if not analysis_input_string:
            # to ensure DEFAULT_ANALYSIS_INPUT is used.
            # keeping it here for robustness in case it's called with an empty string.
            self.update_status_display("WARNING: parse_analysis_input received empty string. Returning 0 iterations.")
            return 0, []

        analysis_groups = re.findall(r'{[^}]+}', analysis_input_string)

        if not analysis_groups:
            self.update_status_display(f"WARNING: No valid analysis groups found in input: '{analysis_input_string}'. Check format.")
            return 0, []
        
        num_iterations = len(analysis_groups)
        return num_iterations, analysis_groups

    def start_processing(self):
        #******kumkumn******
        #Prepare the var_file for this run
        final_var_file_path = self._prepare_run_var_file()
        if not final_var_file_path:
            self.update_status_display("ERROR: Failed to prepare var file. Aborting start processing.")
            return # Stop processing if var file prep failed

       # Get user input for analysis, and apply default if self.analysis_input_text is empty
        user_analysis_input_string = self.analysis_input_text.get("1.0", "end-1c").strip()
        if not user_analysis_input_string:
            self.update_status_display("INFO: No analysis input provided. Using default analysis string.")
            user_analysis_input_string = DEFAULT_ANALYSIS_INPUT

        num_iterations, list_of_analysis_groups = self.parse_analysis_input(user_analysis_input_string)

    # Basic validation after parsing
        if num_iterations == 0:
            self.update_status_display(f"ERROR: No iterations could be parsed from input: '{user_analysis_input_string}'. Aborting.")
            return 

        params = {
            "repo_area": self.repo_area_var.get().strip(), # Renamed
            "eco_work_dir_name": self.eco_work_dir_name_var.get().strip(), # New var for name
            "ip": self.ip_var.get().strip(),
            "chip": self.chip_var.get().strip(),
            "process": self.process_var.get().strip(),
            "var_file": final_var_file_path, #var file path is now the new generated var file
            "block": self.block_var.get().strip(),
            "tool": self.tool_var.get().strip(),
            "analysis_sequences": list_of_analysis_groups, # Storing the list of analyses
            "num_iterations": num_iterations,              # Storing the count
            "timeout": self.timeout_var.get().strip(),
            "interval": self.interval_var.get().strip()
        }
        errors = []
        if not params["repo_area"]: errors.append("Repo Area (for setup) missing.")
        if not params["eco_work_dir_name"]: errors.append("ECO Work Dir Name missing.")
        if not params["ip"]: errors.append("IP Name missing.")
        if not params["chip"]: errors.append("Chip Name missing.")
        if not params["process"]: errors.append("Process missing.")
        if not params["block"]: errors.append("BLOCK Name missing.")
        if not params["var_file"] or not os.path.isfile(params["var_file"]): errors.append("Tool generated Var File missing or invalid.") #******kumkumn****** changed error msg
        if not params["tool"]: errors.append("TOOL missing.")
        try: num_iterations = int(params["num_iterations"]); assert num_iterations >= 0
        except: errors.append("Iterations must be a non-negative int.")
        try: timeout_minutes = int(params["timeout"]); assert timeout_minutes >= 0
        except: errors.append("BOB Timeout must be non-negative int.")
        try: check_interval = int(params["interval"]); assert check_interval >= 1
        except: errors.append("Check Interval must be positive int.")
        if errors: messagebox.showerror("Input Error","\n".join(errors)); return

        abort_flag.clear(); continue_event.clear(); failed_info.update({"is_failed_state": False})
        self.set_controls_state('setup');
        if self.is_log_visible: self.status_text.config(state=tk.NORMAL); self.status_text.delete('1.0',tk.END); self.status_text.config(state=tk.DISABLED)
        self.update_summary_display("Checking Repo Area..."); self.update_status_display(f"INFO: Checking Repo Area: {params['repo_area']}")
        
        repo_area_abs = os.path.abspath(params["repo_area"])
        # Construct the full absolute path for the specific ECO run directory
        eco_run_dir_abs = os.path.join(repo_area_abs, "run", params["eco_work_dir_name"])
        self.update_status_display(f"INFO: Target ECO Work Directory will be: {eco_run_dir_abs}")

        # The "workspace" for bob wa create is the repo_area_abs.
        # The "run" and "repo" subdirs are expected inside repo_area_abs.
        workspace_for_bob_wa_exists = os.path.isdir(os.path.join(repo_area_abs, "run")) and os.path.isdir(os.path.join(repo_area_abs, "repo"))
        
        #get_gui_base_var_file_func = lambda: self.var_file_var.get().strip()
        #******kumkumn******
        get_gui_base_var_file_func = lambda: self._prepare_run_var_file()
        # Arguments for run_eco_logic
        eco_logic_args_tuple = (
            params["var_file"], params["block"], params["tool"], params["analysis_sequences"], num_iterations, 
            eco_run_dir_abs, # Pass the fully resolved absolute path
            timeout_minutes, check_interval, 
            self.update_status_display, self.update_summary_display, self.processing_complete, 
            get_gui_base_var_file_func
        )

        if workspace_for_bob_wa_exists:
            self.update_status_display(f"INFO: BOB 'run'/'repo' subdirs exist in {repo_area_abs}. Skipping 'bob wa create'.")
            self.update_summary_display("Repo Area ready. Starting ECO run...")
            
            # CWD should be <repo_area_abs>/run for bob commands that don't use -r,
            # or if -r points to a relative path from there.
            # For bob commands using -r with an absolute path (like eco_run_dir_abs), CWD is less critical
            # but setting it to repo_area_abs/run is a good practice.
            target_cwd_for_eco = os.path.join(repo_area_abs, "run")
            if not os.path.isdir(target_cwd_for_eco):
                 messagebox.showerror("Error", f"{target_cwd_for_eco} not found. Cannot proceed."); self.set_controls_state('idle'); return
            try: 
                self.update_status_display(f"INFO: Setting CWD to: {target_cwd_for_eco}"); os.chdir(target_cwd_for_eco)
                self.update_status_display(f"INFO: CWD is now: {os.getcwd()}")
            except Exception as e: 
                messagebox.showerror("Error",f"Failed to chdir to {target_cwd_for_eco}: {e}"); self.set_controls_state('idle'); return
            
            self.set_controls_state('running')
            self.processing_thread = threading.Thread(target=run_eco_logic, args=eco_logic_args_tuple, daemon=True); self.processing_thread.start()
        else:
            self.update_status_display(f"INFO: BOB 'run'/'repo' subdirs not in {repo_area_abs}. Starting 'bob wa create' setup...")
            self.update_summary_display("Repo Area setup required...")
            
            # Arguments for run_workspace_setup
            # run_workspace_setup will operate on repo_area_abs
            # It needs setup-specific params (ip, block for wa, chip, process)
            # And it needs the eco_logic_args_tuple to pass to workspace_setup_complete
            setup_specific_params_tuple = (repo_area_abs, params["ip"], params["block"], params["chip"], params["process"])
            
            self.setup_thread = threading.Thread(
                target=self.run_workspace_setup, 
                args=(setup_specific_params_tuple, eco_logic_args_tuple), # Pass as two main arguments
                daemon=True); 
            self.setup_thread.start()

    def run_workspace_setup(self, setup_params_tuple, eco_params_tuple_for_callback):
        repo_area_abs, ip_name_setup, block_name_setup, chip_name_setup, process_name_setup = setup_params_tuple
        try:
            self.update_summary_display("Getting username...")
            # For Python 3.6, use stdout=PIPE, stderr=PIPE and decode manually
            username_res = subprocess.run(['whoami'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=5)
            if username_res.returncode != 0 or not username_res.stdout.strip(): 
                stderr_decoded = username_res.stderr.decode(sys.stderr.encoding or 'utf-8', errors='replace').strip() if username_res.stderr else ""
                raise ValueError(f"'whoami' fail/no output. Code:{username_res.returncode}, Err:{stderr_decoded}")
            username = username_res.stdout.decode(sys.stdout.encoding or 'utf-8', errors='replace').strip()
            email = f"{username}@google.com"; self.update_status_display(f"INFO: Username: {username}")
            
            # bob wa create --area <repo_area_abs>
            cmd = ["bob","wa","create","--area",repo_area_abs,"--ip",ip_name_setup,"--block",block_name_setup,"--chip",chip_name_setup,"--process",process_name_setup]
            self.update_status_display(f"RUNNING: {' '.join(cmd)}"); self.update_summary_display("Running 'bob wa create'...")
            input_str = f"y\n{email}\ny\n"; self.update_status_display(f"INFO: Input: y -> {email} -> y")
            
            # For Python 3.6
            wa_res = subprocess.run(cmd, input=input_str.encode('utf-8'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=150)
            stdout_decoded = wa_res.stdout.decode(sys.stdout.encoding or 'utf-8', errors='replace').strip()
            stderr_decoded = wa_res.stderr.decode(sys.stderr.encoding or 'utf-8', errors='replace').strip()

            if stdout_decoded: self.update_status_display(f"INFO: 'bob wa create' stdout:\n{stdout_decoded}")
            if stderr_decoded: self.update_status_display(f"WARN: 'bob wa create' stderr:\n{stderr_decoded}")
            if wa_res.returncode != 0: raise RuntimeError(f"'bob wa create' failed with exit code {wa_res.returncode}.")
            
            self.update_summary_display("Verifying Repo Area subdirs..."); 
            run_subdir_in_repo = os.path.join(repo_area_abs,"run")
            repo_subdir_in_repo = os.path.join(repo_area_abs,"repo") # This is a bit confusing, bob creates <repo_area_abs>/repo
            time.sleep(1) 
            if not (os.path.isdir(run_subdir_in_repo) and os.path.isdir(repo_subdir_in_repo)): 
                raise RuntimeError(f"'run' and 'repo' subdirs not found in {repo_area_abs} after 'bob wa create'.")
            
            self.update_status_display("INFO: Repo Area setup successful."); self.update_summary_display("Repo Area setup complete. Changing to run dir...")
            try: 
                self.update_status_display(f"INFO: CWD to: {run_subdir_in_repo}"); os.chdir(run_subdir_in_repo)
                self.update_status_display(f"INFO: CWD is now: {os.getcwd()}")
            except Exception as e: raise RuntimeError(f"Failed to chdir to {run_subdir_in_repo} post-setup: {e}")
            
            # Pass the original eco_params_tuple_for_callback which contains all necessary args for run_eco_logic
            self.after(0, self.workspace_setup_complete, eco_params_tuple_for_callback)
        except Exception as e: 
            self.update_status_display(f"ERROR: Repo Area setup failed: {e}")
            self.after(0, self.workspace_setup_failed, f"Setup failed: {e}")
        finally: self.setup_thread = None

    def workspace_setup_complete(self, eco_params_tuple_from_setup): # Receives the tuple
        if not self.winfo_exists(): return
        self.update_summary_display("Starting ECO run..."); self.set_controls_state('running')
        # run_eco_logic will unpack this tuple via *args
        self.processing_thread = threading.Thread(target=run_eco_logic, args=eco_params_tuple_from_setup, daemon=True); 
        self.processing_thread.start()

    def workspace_setup_failed(self, error_message):
        if not self.winfo_exists(): return
        messagebox.showerror("Repo Area Setup Failed", f"Could not set up Repo Area.\nError: {error_message}"); self.update_summary_display("Repo Area setup failed."); self.set_controls_state('idle')
    
    def save_configuration(self):
        cfg = { 
            "repo_area": self.repo_area_var.get(), # Renamed
            "eco_work_dir_name": self.eco_work_dir_name_var.get(), # Added
            "ip_name": self.ip_var.get(),
            "chip_name": self.chip_var.get(),
            "process_name": self.process_var.get(), 
            "base_var_file": self.var_file_var.get(),
            "block_name": self.block_var.get(),
            "tool_name": self.tool_var.get(), 
            "analysis_sequences_name": self.analysis_input_text.get("1.0", "end-1c"),
            "timeout_minutes": self.timeout_var.get(), 
            "check_interval_seconds": self.interval_var.get() 
        }
        fname = filedialog.asksaveasfilename(title="Save Config",defaultextension=".json",filetypes=[("JSON","*.json")])
        if fname: 
            try: 
                with open(fname,'w',encoding='utf-8') as f: json.dump(cfg,f,indent=4)
                self.update_status_display(f"INFO: Config saved: {fname}")
            except Exception as e: messagebox.showerror("Save Error",f"Failed: {e}")
    
    def load_configuration(self):
        fname = filedialog.askopenfilename(title="Load Config",filetypes=[("JSON","*.json")])
        if fname:
            try:
                with open(fname,'r',encoding='utf-8') as f: cfg = json.load(f)
                self.repo_area_var.set(cfg.get("repo_area", DEFAULT_SETUP_REPO_AREA)) # Renamed
                self.eco_work_dir_name_var.set(cfg.get("eco_work_dir_name", DEFAULT_ECO_WORK_DIR_NAME)) # Added
                self.ip_var.set(cfg.get("ip_name", DEFAULT_IP_NAME))
                self.chip_var.set(cfg.get("chip_name",DEFAULT_CHIP_NAME))
                self.process_var.set(cfg.get("process_name",DEFAULT_PROCESS_NODE))
                self.var_file_var.set(cfg.get("base_var_file",""))
                self.block_var.set(cfg.get("block_name",""))
                self.tool_var.set(cfg.get("tool_name","FC"))
                #self.iterations_var.set(cfg.get("iterations","3"))
                # Clear existing content first
                self.analysis_input_text.delete("1.0", "end")
                # Insert the new content
                self.analysis_input_text.insert("1.0", cfg.get("analysis_sequences_name", DEFAULT_ANALYSIS_INPUT))
                self.timeout_var.set(cfg.get("timeout_minutes",str(DEFAULT_BOB_RUN_TIMEOUT_MINUTES)))
                self.interval_var.set(cfg.get("check_interval_seconds",str(DEFAULT_BOB_CHECK_INTERVAL_SECONDS)))
                self.update_status_display(f"INFO: Config loaded: {fname}")
            except Exception as e: messagebox.showerror("Load Error",f"Failed: {e}")
    
    def quit_app(self):
        if (self.processing_thread and self.processing_thread.is_alive()) or \
           (self.setup_thread and self.setup_thread.is_alive()):
            if messagebox.askyesno("Confirm Quit", "Process is running. Quit anyway?"):
                self.update_status_display("PROCESS: Quit requested. Attempting abort...")
                repo_area = self.repo_area_var.get().strip()
                eco_work_dir_name = self.eco_work_dir_name_var.get().strip()
                if repo_area and eco_work_dir_name: # Check if both are available
                    eco_work_dir_to_stop_abs = os.path.join(os.path.abspath(repo_area), "run", eco_work_dir_name)
                    if os.path.isdir(eco_work_dir_to_stop_abs):
                        try: subprocess.run(["bob","stop","-r", eco_work_dir_to_stop_abs],timeout=15,check=False, cwd=os.path.dirname(eco_work_dir_to_stop_abs))
                        except Exception: pass 
                abort_flag.set(); continue_event.set()
                self.after(500, self.destroy) 
        else: self.destroy()

def run_clear_logic(eco_work_dir_to_clear_abs, status_updater, completion_callback): # Parameter is the full path
    status_updater(f"INFO: Attempting to clear ECO Work Dir: {eco_work_dir_to_clear_abs}")
    del_cmd = ["bob", "delete", "-r", eco_work_dir_to_clear_abs, "--all"] # -r uses the full path
    status_updater(f"RUNNING: {' '.join(del_cmd)} \nINFO: Sending 'y' to confirmation prompt...")
    try:
        # CWD for bob delete -r <abs_path> can be anything, but parent of target is safe
        res = subprocess.run(del_cmd, input=b'y\n', check=True, 
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
                             cwd=os.path.dirname(eco_work_dir_to_clear_abs)) 
        stdout_str = res.stdout.decode(sys.stdout.encoding or 'utf-8', errors='replace').strip()
        stderr_str = res.stderr.decode(sys.stderr.encoding or 'utf-8', errors='replace').strip()
        status_updater(f"INFO: 'bob delete' success. Stdout: {stdout_str} Stderr: {stderr_str}")
    except subprocess.TimeoutExpired: status_updater("ERROR: 'bob delete' timed out.")
    except FileNotFoundError: status_updater("ERROR: Command 'bob' not found.")
    except subprocess.CalledProcessError as e: 
        stdout_err_str = e.stdout.decode(sys.stdout.encoding or 'utf-8', errors='replace').strip() if e.stdout else ""
        stderr_err_str = e.stderr.decode(sys.stderr.encoding or 'utf-8', errors='replace').strip() if e.stderr else ""
        status_updater(f"ERROR: 'bob delete' fail (code {e.returncode}) Stdout: {stdout_err_str} Stderr: {stderr_err_str}")
    except Exception as e: status_updater(f"ERROR: Unexpected error during clear: {e}")
    finally:
        if completion_callback: completion_callback()

if __name__ == "__main__":
    abort_flag.clear(); continue_event.clear(); failed_info.update({"is_failed_state": False})
    app = EcoRunnerApp()
    app.protocol("WM_DELETE_WINDOW", app.quit_app)
    app.mainloop()

