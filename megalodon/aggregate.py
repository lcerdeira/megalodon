#!/usr/bin/env python3
import os
import sys
import queue
from time import sleep
import multiprocessing as mp

from tqdm import tqdm

from megalodon import logging, mods, snps, megalodon_helper as mh


#######################################
##### Aggregate SNP and Mod Stats #####
#######################################

def _agg_snps_worker(
        locs_q, snp_stats_q, snp_prog_q, snps_db_fn, write_vcf_llr,
        het_factors, call_mode):
    agg_snps = snps.AggSnps(snps_db_fn, write_vcf_llr)

    while True:
        try:
            snp_loc = locs_q.get(block=False)
        except queue.Empty:
            sleep(0.1)
            continue
        if snp_loc is None:
            break

        snp_var = agg_snps.compute_snp_stats(snp_loc, het_factors, call_mode)
        snp_stats_q.put(snp_var)
        snp_prog_q.put(1)

    return

def _get_snp_stats_queue(
        snp_stats_q, snp_conn, out_dir, ref_names_and_lens, do_sort=False):
    agg_snp_fn = os.path.join(out_dir, mh.OUTPUT_FNS[mh.SNP_NAME])
    if do_sort:
        all_snp_vars = []
    else:
        agg_snp_fp = snps.VcfWriter(
            agg_snp_fn, 'w', ref_names_and_lens=ref_names_and_lens)

    while True:
        try:
            snp_var = snp_stats_q.get(block=False)
            if snp_var is None: continue
            if do_sort:
                all_snp_vars.append(snp_var)
            else:
                agg_snp_fp.write_variant(snp_var)
        except queue.Empty:
            if snp_conn.poll():
                break
            sleep(0.1)
            continue

    while not snp_stats_q.empty():
        snp_var = snp_stats_q.get(block=False)
        if do_sort:
            all_snp_vars.append(snp_var)
        else:
            agg_snp_fp.write_variant(snp_var)

    if do_sort:
        # sort variants and write to file (requires adding __lt__, __gt__
        # methods to variant class
        with snps.VcfWriter(
                agg_snp_fn, 'w',
                ref_names_and_lens=ref_names_and_lens) as agg_snp_fp:
            for snp_var in sorted(all_snp_vars):
                agg_snp_fp.write_variant(snp_var)
    else:
        agg_snp_fp.close()

    return

def _agg_mods_worker(
        locs_q, mod_stats_q, mod_prog_q, mods_db_fn, mod_agg_info):
    agg_mods = mods.AggMods(mods_db_fn, mod_agg_info)

    while True:
        try:
            mod_loc = locs_q.get(block=False)
        except queue.Empty:
            sleep(0.1)
            continue
        if mod_loc is None:
            break

        mod_site = agg_mods.compute_mod_stats(mod_loc)
        mod_stats_q.put(mod_site)
        mod_prog_q.put(1)

    return

def _get_mod_stats_queue(
        mod_stats_q, mod_conn, out_dir, mod_names, ref_names_and_lens,
        do_sort=False):
    agg_mod_fn = os.path.join(out_dir, mh.OUTPUT_FNS[mh.MOD_NAME])
    if do_sort:
        all_mods = []
    else:
        agg_mod_fp = mods.ModVcfWriter(
            agg_mod_fn, mod_names, 'w', ref_names_and_lens=ref_names_and_lens)

    while True:
        try:
            mod_site = mod_stats_q.get(block=False)
            if do_sort:
                all_mods.append(mod_site)
            else:
                agg_mod_fp.write_mod_site(mod_site)
        except queue.Empty:
            if mod_conn.poll():
                break
            sleep(0.1)
            continue

    while not mod_stats_q.empty():
        mod_site = mod_stats_q.get(block=False)
        if do_sort:
            all_mods.append(mod_site)
        else:
            agg_mod_fp.write_mod_site(mod_site)
    if do_sort:
        with mods.ModVcfWriter(
                agg_mod_fn, mod_names, 'w',
                ref_names_and_lens=ref_names_and_lens) as agg_mod_fp:
            for mod_site in sorted(all_mods):
                agg_mod_fp.write_mod_site(mod_site)
    else:
        agg_mod_fp.close()

    return

def _agg_prog_worker(
        snp_prog_q, mod_prog_q, num_snps, num_mods, prog_conn,
        suppress_progress):
    snp_bar, mod_bar = None, None
    if num_snps > 0:
        if num_mods > 0 and not suppress_progress:
            mod_bar = tqdm(desc='Mods', unit=' sites', total=num_mods,
                           position=1, smoothing=0)
            snp_bar = tqdm(desc='SNPs', unit=' sites', total=num_snps,
                           position=0, smoothing=0)
        elif not suppress_progress:
            snp_bar = tqdm(desc='SNPs', unit=' sites', total=num_snps,
                           position=0, smoothing=0)
    elif num_mods > 0 and  not suppress_progress:
        mod_bar = tqdm(desc='Mods', unit=' sites', total=num_mods,
                       position=0, smoothing=0)

    logger = logging.get_logger()
    while True:
        try:
            snp_prog_q.get(block=False)
            if not suppress_progress:
                if snp_bar is not None: snp_bar.update(1)
                if mod_bar is not None: mod_bar.update(0)
        except queue.Empty:
            try:
                mod_prog_q.get(block=False)
                if not suppress_progress:
                    if snp_bar is not None: snp_bar.update(0)
                    if mod_bar is not None: mod_bar.update(1)
            except queue.Empty:
                sleep(0.01)
                if prog_conn.poll():
                    break
                continue

    while not snp_prog_q.empty():
        snp_prog_q.get(block=False)
        if not suppress_progress: snp_bar.update(1)
    while not mod_prog_q.empty():
        mod_prog_q.get(block=False)
        if not suppress_progress: mod_bar.update(1)
    if snp_bar is not None:
        snp_bar.close()
    if mod_bar is not None:
        mod_bar.close()
    if num_mods > 0 and num_snps > 0 and not suppress_progress:
        sys.stderr.write('\n\n')

    return

def _fill_locs_queue(locs_q, db_fn, agg_class, num_ps):
    agg_db = agg_class(db_fn)
    for loc in agg_db.iter_uniq():
        locs_q.put(loc)
    for _ in range(num_ps):
        locs_q.put(None)

    return

def aggregate_stats(
        outputs, out_dir, num_ps, write_vcf_llr, het_factors, call_mode,
        mod_names, mod_agg_info, suppress_progress, ref_names_and_lens):
    if mh.SNP_NAME in outputs and mh.MOD_NAME in outputs:
        num_ps = max(num_ps // 2, 1)

    num_snps, num_mods, snp_prog_q, mod_prog_q = (
        0, 0, queue.Queue(), queue.Queue())
    if mh.SNP_NAME in outputs:
        snps_db_fn = os.path.join(out_dir, mh.OUTPUT_FNS[mh.PR_SNP_NAME][0])
        num_snps = snps.AggSnps(snps_db_fn).num_uniq()
        # create process to collect snp stats from workers
        snp_stats_q, snp_stats_p, main_snp_stats_conn = mh.create_getter_q(
            _get_snp_stats_queue, (out_dir, ref_names_and_lens))
        # create process to fill snp locs queue
        snp_filler_q = mp.Queue(maxsize=mh._MAX_QUEUE_SIZE)
        snp_filler_p = mp.Process(
            target=_fill_locs_queue,
            args=(snp_filler_q, snps_db_fn, snps.AggSnps, num_ps), daemon=True)
        snp_filler_p.start()
        # create worker processes to aggregate snps
        snp_prog_q = mp.Queue(maxsize=mh._MAX_QUEUE_SIZE)
        agg_snps_ps = []
        for _ in range(num_ps):
            p = mp.Process(
                target=_agg_snps_worker,
                args=(snp_filler_q, snp_stats_q, snp_prog_q, snps_db_fn,
                      write_vcf_llr, het_factors, call_mode), daemon=True)
            p.start()
            agg_snps_ps.append(p)

    if mh.MOD_NAME in outputs:
        mods_db_fn = os.path.join(out_dir, mh.OUTPUT_FNS[mh.PR_MOD_NAME][0])
        num_mods = mods.AggMods(mods_db_fn).num_uniq()
        # create process to collect mods stats from workers
        mod_stats_q, mod_stats_p, main_mod_stats_conn = mh.create_getter_q(
            _get_mod_stats_queue, (out_dir, mod_names, ref_names_and_lens))
        # create process to fill mod locs queue
        mod_filler_q = mp.Queue(maxsize=mh._MAX_QUEUE_SIZE)
        mod_filler_p = mp.Process(
            target=_fill_locs_queue,
            args=(mod_filler_q, mods_db_fn, mods.AggMods, num_ps), daemon=True)
        mod_filler_p.start()
        # create worker processes to aggregate mods
        mod_prog_q = mp.Queue(maxsize=mh._MAX_QUEUE_SIZE)
        agg_mods_ps = []
        for _ in range(num_ps):
            p = mp.Process(
                target=_agg_mods_worker,
                args=(mod_filler_q, mod_stats_q, mod_prog_q, mods_db_fn,
                      mod_agg_info),
                daemon=True)
            p.start()
            agg_mods_ps.append(p)

    # create progress process
    logger = logging.get_logger('agg')
    logger.info(
        'Aggregating {} SNPs and {} mod sites over reads.'.format(
            num_snps, num_mods))
    main_prog_conn, prog_conn = mp.Pipe()
    prog_p = mp.Process(
        target=_agg_prog_worker,
        args=(snp_prog_q, mod_prog_q, num_snps, num_mods, prog_conn,
              suppress_progress),
        daemon=True)
    prog_p.start()

    # join filler processes first
    if mh.SNP_NAME in outputs:
        snp_filler_p.join()
        for agg_snps_p in agg_snps_ps:
            agg_snps_p.join()
        # send to conn
        if snp_stats_p.is_alive():
            main_snp_stats_conn.send(True)
        snp_stats_p.join()
    if mh.MOD_NAME in outputs:
        for agg_mods_p in agg_mods_ps:
            agg_mods_p.join()
        if mod_stats_p.is_alive():
            main_mod_stats_conn.send(True)
        mod_stats_p.join()
    if prog_p.is_alive():
        main_prog_conn.send(True)
        prog_p.join()

    return


if __name__ == '__main__':
    sys.stderr.write('This is a module. See commands with `megalodon -h`')
    sys.exit(1)
