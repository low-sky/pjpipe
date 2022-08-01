import os
import socket

from jwst_nircam_reprocess import NircamReprocess

host = socket.gethostname()

if 'node' in host:
    raw_dir = '/data/beegfs/astro-storage/groups/schinnerer/williams/jwst_data'
    working_dir = '/data/beegfs/astro-storage/groups/schinnerer/williams/jwst_working'
else:
    raw_dir = '/Users/williams/Documents/phangs/jwst_data'
    working_dir = '/Users/williams/Documents/phangs/jwst_working'

reprocess_dir = os.path.join(working_dir, 'nircam_lev3_reprocessed')
crds_path = os.path.join(working_dir, 'crds')

reprocess_dir_ext = 'v0p2'

reprocess_dir += '_%s' % reprocess_dir_ext

galaxies = [
    # 'ngc0628',
    'ngc7320',
    # 'ngc7496',
]

for galaxy in galaxies:

    alignment_fits = {'ngc0628': 'hlsp_phangs-hst_hst_acs-wfc_ngc628mosaic_f814w_v1_exp-drc-sci.fits',
                      'ngc7320': 'hlsp_sm4ero_hst_wfc3_11502-ngc7318_f814w_v1_sci_drz.fits',
                      'ngc7496': 'hlsp_phangs-hst_hst_wfc3-uvis_ngc7496_f814w_v1_exp-drc-sci.fits',
                      }[galaxy]
    alignment_image = os.path.join(working_dir,
                                   'alignment_images',
                                   alignment_fits)

    nc_reproc = NircamReprocess(crds_path=crds_path,
                                galaxy=galaxy,
                                raw_dir=raw_dir,
                                reprocess_dir=reprocess_dir,
                                do_all=True,
                                astrometric_alignment_image=alignment_image,
                                )
    nc_reproc.run_all()

print('Complete!')
