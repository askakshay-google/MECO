import argparse, re, os, sys, math, getpass, time, os , shutil
import os.path
import glob
import collections

def gc_process(fs_file):
    if os.path.exists(fs_file):
        with open (fs_file, encoding ='utf-8' , newline="") as fsp:
            data = []
            for line in fsp:
                row = line.strip().split()
                if "=>" in row:
                    if all(row):
                        try:
                            row[5] = float(row[5])
                            data.append(row)
                        except ValueError:
                            pass
            data.sort(key=lambda x: x[5])
            seen_fifth_columns = set()
            unique_data = []
            for row in data:
                if row[4] not in seen_fifth_columns:
                    seen_fifth_columns.add(row[4])
                    unique_data.append(row)
            unique_data.sort(key=lambda x: x[5])
            return unique_data

def gc_summarize(data,ofile):
    first_column_values = [row[0] for row in data]
    value_counts = {}
    for value in first_column_values:
        value_counts[value] = value_counts.get(value, 0) + 1
    total_rows = len(data)
    cumulative_percentage = 0
    sorted_items = sorted(value_counts.items(), key=lambda item: item[1], reverse=True)
    with open (ofile,'a',encoding ='utf-8' , newline="") as of:
        for value, count in sorted_items:
            percentage = (count / total_rows) * 100
            cumulative_percentage += percentage
            value=re.sub(".gz","",value)
            print(f"{value:>50} | {count:>15} | {percentage: > 13.2f}% | {cumulative_percentage: > 10.2f}%",file=of)
        print("#"*100,file=of)
        print("",file=of)
        print("",file=of)

def gc_dsa(merge_dir,output_dir):
    timing_summary=output_dir+"/timing_summary.txt"
    with open (timing_summary, 'a',encoding ='utf-8' , newline="") as ts:
        fs_file=merge_dir+"/timing_merge/func_merge/setup_merge/ALL_merge/reg_2_reg_max_paths_fsep"
        fs_data=gc_process(fs_file)
        print("#"*100,file=ts)
        print(f"Func setup:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","Vio FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize(fs_data,timing_summary)

    with open (timing_summary, 'a',encoding ='utf-8' , newline="") as ts:
        fh_file=merge_dir+"/timing_merge/func_merge/hold_merge/ALL_merge/reg_2_reg_min_paths_fsep"
        fh_data=gc_process(fh_file)
        print("#"*100,file=ts)
        print(f"Func hold:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","Vio FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize(fh_data,timing_summary)

    with open (timing_summary, 'a',encoding ='utf-8' , newline="") as ts:
        ss_file=merge_dir+"/timing_merge/shift_merge/setup_merge/ALL_merge/reg_2_reg_max_paths_fsep"
        ss_data=gc_process(ss_file)
        print("#"*100,file=ts)
        print(f"Shift setup:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","Vio FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize(ss_data,timing_summary)

    with open (timing_summary, 'a',encoding ='utf-8' , newline="") as ts:
        sh_file=merge_dir+"/timing_merge/shift_merge/hold_merge/ALL_merge/reg_2_reg_min_paths_fsep"
        sh_data=gc_process(sh_file)
        print("#"*100,file=ts)
        print(f"Shift hold:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","VIO FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize(sh_data,timing_summary)

def gc_process_tdrc(mp_dir,c):
    if re.search('1',c):
        data = []
        for root, dirs, files in os.walk(mp_dir):
            for file1 in files:
                filename=mp_dir+"/"+file1
                if re.search(".csv",filename):
                    with open(filename, 'r') as f:
                        for line in f:
                            last_field = line.replace(',', ' ').split()[-1]
                            if last_field != "Scenario":
                                data.append(last_field)
        counts = collections.Counter(data)
        total_rows=len(data)
        sorted_counts = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        return sorted_counts , total_rows
    if re.search('2',c):
        data = []
        for root, dirs, files in os.walk(mp_dir):
            for file1 in files:
                filename=mp_dir+"/"+file1
                if re.search(".csv",filename):
                    with open(filename, 'r') as f:
                        for line in f:
                            last_field = line.replace(',', ' ').split()[-2]
                            if last_field != "scenario":
                                data.append(last_field)
        counts = collections.Counter(data)
        total_rows=len(data)
        sorted_counts = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        return sorted_counts , total_rows

def gc_summarize_tdrc(mp_data,ofile,total_rows):
    cumulative_percentage = 0
    with open (ofile,'a',encoding ='utf-8' , newline="") as of:
        for value, count in mp_data:
            percentage = (count / total_rows) * 100
            cumulative_percentage += percentage
            value=re.sub(".gz","",value)
            print(f"{value:>50} | {count:>15} | {percentage: > 13.2f}% | {cumulative_percentage: > 10.2f}%",file=of)
        print("#"*100,file=of)
        print("",file=of)
        print("",file=of)

def gc_dsa_tdrc(merge_dir,output_dir):
    tdrc_summary=output_dir+"/tdrc_summary.txt"
    with open (tdrc_summary, 'a',encoding ='utf-8' , newline="") as ts:
        mp_dir=merge_dir+"/tdrc_merge/min_period"
        mp_data,t=gc_process_tdrc(mp_dir,"1")
        print("#"*100,file=ts)
        print(f"Min Period:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","Vio FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize_tdrc(mp_data,tdrc_summary,t)

    with open (tdrc_summary, 'a',encoding ='utf-8' , newline="") as ts:
        mp_dir=merge_dir+"/tdrc_merge/min_pulse_width"
        mp_data,t=gc_process_tdrc(mp_dir,"1")
        print("#"*100,file=ts)
        print(f"Min Pulse Width:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","Vio FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize_tdrc(mp_data,tdrc_summary,t)

    with open (tdrc_summary, 'a',encoding ='utf-8' , newline="") as ts:
        mp_dir=merge_dir+"/tdrc_merge/max_trans/data"
        mp_data,t=gc_process_tdrc(mp_dir,"2")
        print("#"*100,file=ts)
        print(f"Max Trans Data:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","Vio FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize_tdrc(mp_data,tdrc_summary,t)

    with open (tdrc_summary, 'a',encoding ='utf-8' , newline="") as ts:
        mp_dir=merge_dir+"/tdrc_merge/max_trans/clock"
        mp_data,t=gc_process_tdrc(mp_dir,"2")
        print("#"*100,file=ts)
        print(f"Max Trans Clock:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","Vio FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize_tdrc(mp_data,tdrc_summary,t)

    with open (tdrc_summary, 'a',encoding ='utf-8' , newline="") as ts:
        mp_dir=merge_dir+"/tdrc_merge/max_cap/data"
        mp_data,t=gc_process_tdrc(mp_dir,"1")
        print("#"*100,file=ts)
        print(f"Max Cap Data:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","VIO FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize_tdrc(mp_data,tdrc_summary,t)

    with open (tdrc_summary, 'a',encoding ='utf-8' , newline="") as ts:
        mp_dir=merge_dir+"/tdrc_merge/max_cap/clock"
        mp_data,t=gc_process_tdrc(mp_dir,"1")
        print("#"*100,file=ts)
        print(f"Max Cap Clock:",file=ts)
        print("{:^50} | {:^15} | {:^15}| {:^10}".format("Corner","# FEP","VIO FEP Pct","Total Pct") ,file=ts)
        print("_"*100,file=ts)
    gc_summarize_tdrc(mp_data,tdrc_summary,t)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--merge_dir' , help='merge_dir')
    parser.add_argument('--output_dir' , help='output_dir')
    args = parser.parse_args()
    merge_dir=args.merge_dir
    output_dir=args.output_dir
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    gc_dsa(merge_dir,output_dir)
    gc_dsa_tdrc(merge_dir,output_dir)
