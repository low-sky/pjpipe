# Read and convolve all of current JWST images to F2100W matched
# resolution as well as a few common round Gaussian resolutions.

import glob, os

from astropy.io import fits

# This module holds most of the key routines

from utils_jwst import *

# This module holds the control flow tags

from postprocess_control_flow import *

# -----------------------------------------------------------------------------------
# Loop over all galaxies and do the initial convolution 
# -----------------------------------------------------------------------------------

for this_gal in gal_names:

    if len(just_targets) > 0:
        if this_gal not in just_targets:
            continue
    
    template_filter = 'F2100W'
    template_mask = (image_key['galaxy'] == this_gal)* \
        (image_key['filter'] == template_filter)
    template_file = my_input_root+str(np.array(image_key[template_mask]['filename'])[0])
    if os.path.isfile(template_file) == False:
        print("No valid template for ", template_file)
        continue
    template_header = fits.open(template_file)['SCI'].header
    
    for this_filt in filters:

        # Select the relevant table row
        if np.sum((image_key['galaxy'] == this_gal)*(image_key['filter'] == this_filt)) == 0:
            print("No match for: ", this_gal, this_filt)
            continue
        tab_mask = (image_key['galaxy'] == this_gal)*(image_key['filter'] == this_filt)
        input_file = my_input_root+str(np.array(image_key[tab_mask]['filename'])[0])
        print(this_gal, this_filt, input_file)

        # Check that the input file is present
        if os.path.isfile(input_file) == False:
            print("Input file not found, skipping. ", input_file)
            continue

        # Read the input science image
        input_hdu = fits.open(input_file)['SCI']

        # Identify names of kernels
        kern_to_f2100w = my_kern_dir+this_filt+'_to_F2100W.fits'
        kern_to_gauss = my_kern_dir+this_filt+ext_first_gauss

        # ---------------------
        # Convolve to F2100W
        # ---------------------        

        if do_conv_to_f2100w_initial:
        
            output_file_name = my_initial_matched_res_dir + \
                this_gal + '_'+this_filt+'_atF2100W.fits'
            print("... building ", output_file_name)
            if this_filt != 'F2100W':
                kernel_hdu = fits.open(kern_to_f2100w)[0]
                convolved_hdu = conv_with_kernel(
                    input_hdu, kernel_hdu,
                    outfile=output_file_name, overwrite=True)

                aligned_hdu = align_image(
                    convolved_hdu, template_header, hdu_in=0,
                    order='bilinear', missing_value=np.nan,
                    outfile=None, overwrite=True)

                aligned_hdu.writeto(output_file_name, overwrite=True)            
            else:
                input_hdu.writeto(output_file_name, overwrite=True)
            
        # ---------------------
        # Convolve to Gaussian
        # ---------------------        

        if do_conv_to_gauss_initial:
        
            output_file_name = my_initial_matched_res_dir + \
                this_gal + '_'+this_filt+'_'+label_first_gauss+'.fits'
            print("... building ", output_file_name)
            kernel_hdu = fits.open(kern_to_gauss)[0]
            convolved_hdu = conv_with_kernel(
                input_hdu, kernel_hdu,
                outfile=output_file_name, overwrite=True)
            convolved_hdu.writeto(output_file_name, overwrite=True)

            # Loop over key comparison Gaussians
            for this_label in comp_gauss_dict.keys():
                
                this_fwhm = comp_gauss_dict[this_label]
                this_oversamp = comp_gauss_oversamp_dict[this_label]
                
                output_file_name = my_initial_matched_res_dir + \
                    this_gal + '_'+this_filt+'_'+this_label+'.fits'
                
                print("... building ", output_file_name)
                print("... kernel ", this_label, this_fwhm)
                kernel_hdu = make_gaussian_psf(
                    fwhm_arcsec = np.sqrt(this_fwhm**2-(fwhm_first_gauss)**2),
                    oversample_by=this_oversamp, outfile=None)
                
                convolved_more_hdu = conv_with_kernel(
                    convolved_hdu, kernel_hdu,
                    outfile=output_file_name, overwrite=True)
                convolved_more_hdu.writeto(output_file_name, overwrite=True)
