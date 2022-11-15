import glob

import os
import socket

from jwst_reprocess import JWSTReprocess

host = socket.gethostname()

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

with open('config.toml','rb') as f:
    config = tomllib.load(f)

with open('local.toml','rb') as f:
    local = tomllib.load(f)

script_dir = os.getcwd()

raw_dir = local['local']['raw_dir']
working_dir = local['local']['working_dir']
updated_flats_dir = local['local']['updated_flats_dir']
if updated_flats_dir == '':
    updated_flats_dir = None

# We may want to occasionally flush out the CRDS directory to avoid weirdness between mappings. Probably do this at
# the start of another version cycle
flush_crds = config['pipeline']['flush_crds']

if 'pmap' in config['pipeline']['crds_context']:
    os.environ['CRDS_CONTEXT'] = config['pipeline']['crds_context']


reprocess_dir = os.path.join(working_dir, 'jwst_lv3_reprocessed')
crds_dir = os.path.join(working_dir, 'crds')

if flush_crds:
    os.system('rm -rf %s' % crds_dir)
    os.makedirs(crds_dir)

reprocess_dir_ext = config['pipeline']['data_version']

reprocess_dir += '_%s' % reprocess_dir_ext

galaxies = config['galaxies']['targets']

for galaxy in galaxies:
    alignment_table_name = config['alignment'][galaxy]
    alignment_table = os.path.join(script_dir,
                                   'alignment',
                                   alignment_table_name)

    # We can't use NIRCAM bands for IC5332
    if galaxy in ['ic5332']:
        alignment_mapping = {
            'F1000W': 'F770W',  # Step up MIRI wavelengths
            'F1130W': 'F1000W',
            'F2100W': 'F1130W',
        }
    else:
        alignment_mapping = config['alignment_mapping']

    bands = (config['pipeline']['nircam_bands'] + 
             config['pipeline']['miri_bands'])
    cur_field = config['pipeline']['lev3_fields']
    if cur_field == []:
        cur_field = None  
    reproc = JWSTReprocess(galaxy=galaxy,
                           raw_dir=raw_dir,
                           reprocess_dir=reprocess_dir,
                           crds_dir=crds_dir,
                           astrometric_alignment_type='table',
                           astrometric_alignment_table=alignment_table,
                           alignment_mapping=alignment_mapping,
                           bands=bands,
                           procs=local['local']['processors'],
                           overwrite_all=config['overwrite']['all'],
                           overwrite_lv1=config['overwrite']['lv1'],
                           overwrite_lv2=config['overwrite']['lv2'],
                           overwrite_lyot_adjust=config['overwrite']['lyot_adjust'],
                           overwrite_lv3=config['overwrite']['lv3'],
                           overwrite_astrometric_alignment=config['overwrite']['astrometric_alignment'],
                           overwrite_astrometric_ref_cat=config['overwrite']['astrometric_ref_cat'],
                           lv1_parameter_dict=config['lv1_parameters'],
                           lv2_parameter_dict=config['lv2_parameters'],
                           lv3_parameter_dict=config['lv3_parameters'],
                           updated_flats_dir=updated_flats_dir,
                           do_lyot_adjust=config['pipeline']['lyot_adjust'],
                           # process_bgr_like_science=False,
                           use_field_in_lev3=cur_field
                           )
    reproc.run_all()



print('Complete!')
