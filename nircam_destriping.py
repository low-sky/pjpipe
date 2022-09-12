import copy
import os
import pickle

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from mpl_toolkits.axes_grid1 import make_axes_locatable
from photutils import make_source_mask
from scipy.ndimage import binary_dilation
from scipy.ndimage import median_filter
from scipy.stats import median_abs_deviation
from skimage import filters

import pca.vwpca as vw
import pca.vwpca_normgappy as gappy

DESTRIPING_METHODS = ['row_median', 'median_filter', 'pca', 'pca+median']

matplotlib.rcParams['mathtext.fontset'] = 'stix'
matplotlib.rcParams['font.family'] = 'STIXGeneral'
matplotlib.rcParams['font.size'] = 14

matplotlib.use('Agg')


class NircamDestriper:

    def __init__(self,
                 hdu_name=None,
                 hdu_out_name=None,
                 quadrants=True,
                 destriping_method='row_median',
                 sigma=3,
                 npixels=3,
                 dilate_size=11,
                 max_iters=20,
                 median_filter_scales=None,
                 pca_components=50,
                 pca_reconstruct_components=10,
                 pca_diffuse=False,
                 pca_final_med_row_subtraction=False,
                 pca_file=None,
                 just_sci_hdu=False,
                 plot_dir=None,
                 ):
        """NIRCAM Destriping routines

        Contains a number of routines to destripe NIRCAM data. For now, it's unclear what the best way forward is (and
        indeed, it may be different for different datasets), so there are a number of routines

        Args:
            * hdu_name (str): Input name for fits HDU
            * hdu_out_name (str): Output name for HDU
            * quadrants (bool): Whether to split the chip into 512 pixel segments, and destripe each (mostly)
                separately. Defaults to True
            * destriping_method (str): Method to use for destriping. Allowed options are given by DESTRIPING_METHODS
            * sigma (float): Sigma for sigma-clipping. Defaults to 3
            * npixels (int): Pixels to grow for masking. Defaults to 5
            * dilate_size (int): make_source_mask dilation size. Defaults to 11
            * max_iters (int): Maximum sigma-clipping iterations. Defaults to 20
            * median_filter_scales (list): Scales for median filtering
            * pca_components (int): Number of PCA components to model. Defaults to 50
            * pca_reconstruct_components (int): Number of PCA components to use in reconstruction. Defaults to 10
            * pca_diffuse (bool): Whether to perform high-pass filter on data before PCA, to remove diffuse, extended
                emission. Defaults to False, but should be set True for observations where emission fills the FOV
            * pca_final_med_row_subtraction (bool): Whether to perform a final row-by-row median subtraction from the
                data after PCA. Generally this isn't needed, but may be required for the shortest filters. Defaults to
                False.
            * pca_file (str): Path to save PCA model to (should be a .pkl file). If using quadrants, it will append
                the quadrant number accordingly automatically. Defaults to None, which means will not save out files
            * just_sci_hdu (bool): Write full fits HDU, or just SCI? Useful for testing, defaults to False
            * plot_dir (str): Directory to save diagnostic plots to. Defaults to None, which will not save any plots
        """

        if hdu_name is None:
            raise Warning('hdu_name should be defined!')

        if destriping_method not in DESTRIPING_METHODS:
            raise Warning('destriping_method should be one of %s' % DESTRIPING_METHODS)

        self.hdu_name = hdu_name
        self.hdu = fits.open(self.hdu_name, memmap=False)

        if hdu_out_name is None:
            hdu_out_name = self.hdu_name.replace('.fits', '_destriped.fits')
        self.hdu_out_name = hdu_out_name

        self.just_sci_hdu = just_sci_hdu

        self.quadrants = quadrants

        self.destriping_method = destriping_method

        if median_filter_scales is None:
            median_filter_scales = [3, 7, 15, 31, 63, 127]
        self.median_filter_scales = median_filter_scales

        self.sigma = sigma
        self.npixels = npixels
        self.max_iters = max_iters
        self.dilate_size = dilate_size

        self.pca_components = pca_components
        self.pca_reconstruct_components = pca_reconstruct_components
        self.pca_diffuse = pca_diffuse
        self.pca_final_med_row_subtraction = pca_final_med_row_subtraction
        self.pca_file = pca_file

        self.plot_dir = plot_dir

    def run_destriping(self):

        if self.destriping_method == 'row_median':
            self.run_row_median()
        elif self.destriping_method == 'median_filter':
            self.run_median_filter()
        elif self.destriping_method in ['pca', 'pca+median']:
            self.run_pca_denoise()
        else:
            raise NotImplementedError('Destriping method %s not yet implemented!' % self.destriping_method)

        if self.just_sci_hdu:
            self.hdu['SCI'].writeto(self.hdu_out_name,
                                    overwrite=True)
        else:
            self.hdu.writeto(self.hdu_out_name,
                             overwrite=True)

        self.hdu.close()

    def run_pca_denoise(self,
                        ):
        """PCA-based de-noising

        Build a PCA model for the noise using the robust PCA implementation from Tamas Budavari and Vivienne Wild. We
        mask the data, optionally high-pass filter (Butterwoth) to remove extended diffuse emission, and build the PCA
        model from there. If pca+median is selected, we do a final row-by-row median subtraction at the end, to catch
        the low-frequency striping the Butterworth filter will strip out

        """

        zero_idx = np.where(self.hdu['SCI'].data == 0)

        quadrant_size = self.hdu['SCI'].data.shape[1] // 4

        self.hdu['SCI'].data[zero_idx] = np.nan

        self.hdu['SCI'].data = self.level_data(self.hdu['SCI'].data)

        mask = make_source_mask(self.hdu['SCI'].data,
                                nsigma=self.sigma,
                                npixels=self.npixels,
                                dilate_size=self.dilate_size,
                                # dilate_size=1,
                                sigclip_iters=self.max_iters,
                                )

        dq_mask = ~np.isfinite(self.hdu['SCI'].data) | (self.hdu['SCI'].data == 0) | (self.hdu['DQ'].data != 0)
        mask = mask | dq_mask

        data = copy.deepcopy(self.hdu['SCI'].data)
        err = copy.deepcopy(self.hdu['ERR'].data)
        original_mask = copy.deepcopy(mask)

        # Trim off the 0 rows/cols
        data = data[4:-4, 4:-4]
        err = err[4:-4, 4:-4]
        dq_mask = dq_mask[4:-4, 4:-4]
        mask = mask[4:-4, 4:-4]

        data_mean, data_med, data_std = sigma_clipped_stats(data,
                                                            mask=mask,
                                                            sigma=self.sigma,
                                                            maxiters=self.max_iters,
                                                            )

        data -= data_med

        # data_fit = copy.deepcopy(data)

        if self.pca_diffuse:

            # Make a high signal-to-noise mask, because these guys will cause bad ol' negative bowls
            high_sn_mask = data > 10 * data_std
            high_sn_mask = binary_dilation(high_sn_mask, iterations=1)

            # Replace bad data with random noise
            idx = np.where(np.isnan(data) | dq_mask | high_sn_mask)
            data[idx] = np.random.normal(loc=0, scale=data_std, size=len(idx[0]))

            # Pad out the data by reflection to avoid ringing at boundaries
            data_pad = np.zeros([data.shape[0] * 2, data.shape[1] * 2])
            data_pad[:data.shape[0], :data.shape[1]] = copy.deepcopy(data)
            data_pad[-data.shape[0]:, -data.shape[1]:] = copy.deepcopy(data[::-1, ::-1])
            data_pad[-data.shape[0]:, :data.shape[1]] = copy.deepcopy(data[::-1, :])
            data_pad[:data.shape[0], -data.shape[1]:] = copy.deepcopy(data[:, ::-1])
            data_pad = np.roll(data_pad, axis=[0, 1], shift=[data.shape[0] // 2, data.shape[1] // 2])
            data_pad = data_pad[data.shape[0] // 4:-data.shape[0] // 4, data.shape[1] // 4:-data.shape[1] // 4]

            # Filter the image to remove any large scale structure.
            data_filter = filters.butterworth(data_pad,
                                              high_pass=True,
                                              # order=0.1,
                                              # cutoff_frequency_ratio=0.01,
                                              )
            data_filter = data_filter[data.shape[0] // 4:-data.shape[0] // 4, data.shape[1] // 4:-data.shape[1] // 4]
            data_filter[idx] = np.random.normal(loc=0, scale=data_std, size=len(idx[0]))

            data_train = copy.deepcopy(data_filter)

            train_med = sigma_clipped_stats(data_train,
                                            mask=high_sn_mask,
                                            sigma=self.sigma,
                                            maxiters=self.max_iters,
                                            )[1]

            # Make a mask from this filtered data. Use absolute values to catch negatives too
            mask_train = make_source_mask(np.abs(data_train - train_med),
                                          mask=high_sn_mask,
                                          nsigma=self.sigma,
                                          npixels=self.npixels,
                                          dilate_size=self.dilate_size,
                                          sigclip_iters=self.max_iters,
                                          )
            mask_train = mask_train | high_sn_mask

            if self.plot_dir is not None:
                plot_name = os.path.join(self.plot_dir,
                                         self.hdu_out_name.split(os.path.sep)[-1].replace('.fits', '_filter+mask.png')
                                         )

                vmin, vmax = np.nanpercentile(data_train, [1, 99])
                plt.figure(figsize=(8, 4))
                plt.subplot(1, 2, 1)
                plt.imshow(data_train, origin='lower', vmin=vmin, vmax=vmax)

                plt.axis('off')

                plt.title('Filtered Data')

                plt.subplot(1, 2, 2)
                plt.imshow(mask_train, origin='lower', interpolation='none')

                plt.axis('off')

                plt.title('Mask')

                plt.savefig(plot_name, bbox_inches='tight')

                # plt.show()
                plt.close()

        else:

            data_train = copy.deepcopy(data)
            mask_train = copy.deepcopy(mask)

        if self.quadrants:

            noise_model_arr = np.zeros_like(data)

            # original_data = data_train[mask_train]
            data_train[mask_train] = np.nan

            data_med = np.nanmedian(data_train, axis=1)

            for i in range(4):

                if i == 0:
                    idx_slice = slice(0, quadrant_size - 4)
                elif i == 3:
                    idx_slice = slice(1532, 2040)
                else:
                    idx_slice = slice(i * quadrant_size - 4, (i + 1) * quadrant_size - 4)

                data_quadrant = copy.deepcopy(data_train[:, idx_slice])
                mask_quadrant = copy.deepcopy(mask_train[:, idx_slice])
                err_quadrant = copy.deepcopy(err[:, idx_slice])

                norm_factor = np.abs(np.diff(np.nanpercentile(data_quadrant, [16, 84]))[0])
                norm_median = np.nanmedian(data_quadrant)

                data_quadrant = (data_quadrant - norm_median) / norm_factor + 1
                err_quadrant /= norm_factor

                # Replace NaNd data with column median
                for col in range(data_quadrant.shape[0]):
                    idx = np.where(np.isnan(data_quadrant[col, :]))
                    data_quadrant[col, idx[0]] = (data_med[col] - norm_median) / norm_factor + 1

                # data_quadrant[mask_quadrant] = 0
                err_quadrant[mask_quadrant] = 0

                if self.pca_file is not None:
                    pca_file = self.pca_file.replace('.pkl', '_amp_%d.pkl' % i)
                else:
                    pca_file = None

                if self.pca_file is not None and os.path.exists(pca_file):
                    with open(pca_file, 'rb') as f:
                        eigen_system_dict = pickle.load(f)
                else:
                    eigen_system_dict = self.fit_robust_pca(data_quadrant,
                                                            err_quadrant,
                                                            mask_quadrant,
                                                            )
                    if pca_file is not None:
                        with open(pca_file, 'wb') as f:
                            pickle.dump(eigen_system_dict, f)

                noise_model = self.reconstruct_pca(eigen_system_dict,
                                                   data_quadrant,
                                                   err_quadrant,
                                                   mask_quadrant)

                noise_model = (noise_model.T - 1) * norm_factor + norm_median
                noise_model_arr[:, idx_slice] = copy.deepcopy(noise_model)

            full_noise_model = np.full_like(self.hdu['SCI'].data, np.nan)
            full_noise_model[4:-4, 4:-4] = copy.deepcopy(noise_model_arr)

        else:

            data_train[mask_train] = np.nan
            err_train = copy.deepcopy(err)

            norm_mad = median_abs_deviation(data_train, axis=None, nan_policy='omit')

            data_train = data_train / norm_mad + 1
            err_train /= norm_mad

            data_train[mask_train] = 0
            err_train[mask_train] = 0

            if self.pca_file is not None and os.path.exists(self.pca_file):
                with open(self.pca_file, 'rb') as f:
                    eigen_system_dict = pickle.load(f)
            else:
                eigen_system_dict = self.fit_robust_pca(data_train,
                                                        err_train,
                                                        mask_train,
                                                        )
                if self.pca_file is not None:
                    with open(self.pca_file, 'wb') as f:
                        pickle.dump(eigen_system_dict, f)

            noise_model = self.reconstruct_pca(eigen_system_dict,
                                               data_train,
                                               err_train,
                                               mask_train)

            noise_model = (noise_model.T - 1) * norm_mad

            full_noise_model = np.full_like(self.hdu['SCI'].data, np.nan)
            full_noise_model[4:-4, 4:-4] = copy.deepcopy(noise_model)

        if self.destriping_method == 'pca+median':
            # Centre the noise model around 0 to preserve flux
            noise_med = sigma_clipped_stats(full_noise_model,
                                            mask=original_mask,
                                            sigma=self.sigma,
                                            maxiters=self.max_iters,
                                            )[1]
            full_noise_model -= noise_med

        if self.pca_final_med_row_subtraction:

            # Do a final, row-by-row clipped median subtraction

            med = sigma_clipped_stats(data=self.hdu['SCI'].data - full_noise_model,
                                      mask=original_mask,
                                      sigma=self.sigma,
                                      maxiters=self.max_iters,
                                      axis=1
                                      )[1]

            nan_idx = np.where(~np.isfinite(med))

            # We expect there to be 8 NaNs here, if there's more it's an issue
            if len(nan_idx[0]) > 8:
                raise Warning('Median failing -- likely too much masked data')

            # pca_med_array = np.ma.array(data=self.hdu['SCI'].data - full_noise_model,
            #                             mask=original_mask)
            #
            # med = np.ma.median(pca_med_array, axis=1)
            # med = med.data

            med[~np.isfinite(med)] = 0
            med -= np.nanmedian(med)

            full_noise_model += med[:, np.newaxis]

        self.hdu['SCI'].data -= full_noise_model

        self.hdu['SCI'].data[zero_idx] = 0

        if self.plot_dir is not None:
            plot_name = os.path.join(self.plot_dir,
                                     self.hdu_out_name.split(os.path.sep)[-1].replace('.fits', '_noise_model.png'),
                                     )

            vmin, vmax = np.nanpercentile(full_noise_model, [1, 99])
            vmin_data, vmax_data = np.nanpercentile(self.hdu['SCI'].data, [1, 99])

            plt.figure(figsize=(8, 4))

            ax = plt.subplot(1, 2, 1)
            im = plt.imshow(full_noise_model, origin='lower', vmin=vmin, vmax=vmax)
            plt.axis('off')

            plt.title('Noise Model')

            divider = make_axes_locatable(ax)
            cax = divider.append_axes('left', size='5%', pad=0)

            plt.colorbar(im, cax=cax, label='MJy/sr')

            cax.yaxis.set_ticks_position('left')
            cax.yaxis.set_label_position('left')

            ax = plt.subplot(1, 2, 2)
            im = plt.imshow(self.hdu['SCI'].data, origin='lower', vmin=vmin_data, vmax=vmax_data)
            plt.axis('off')

            plt.title('Destriped Data')

            divider = make_axes_locatable(ax)
            cax = divider.append_axes('right', size='5%', pad=0)

            plt.colorbar(im, cax=cax, label='MJy/sr')

            plt.show()

            plt.savefig(plot_name, bbox_inches='tight')
            plt.close()

    def fit_robust_pca(self,
                       data,
                       err,
                       mask,
                       mask_column_frac=0.25,
                       min_column_frac=0.5,
                       ):

        # In low masked cases, take the data where less than mask_column_frac is masked. In highly masked cases, take
        # min_column_frac of data to ensure we have enough to fit.
        min_n_cols = int(data.shape[1] * min_column_frac)
        mask_sum = np.sum(mask, axis=0)
        low_masked_cols = len(np.where(mask_sum < mask_column_frac * data.shape[0])[0])
        n_cols = np.max([low_masked_cols, min_n_cols])

        mask_idx = np.argsort(mask_sum)
        data_low_emission = data[:, mask_idx[:n_cols]]
        err_low_emission = err[:, mask_idx[:n_cols]]

        # Roll around the axis to avoid learning where the mask is
        for i in range(1):
            roll_idx = np.random.randint(low=0, high=data.shape[0], size=data.shape[1])
            data_roll = np.roll(data_low_emission, shift=roll_idx, axis=0)
            err_roll = np.roll(err_low_emission, shift=roll_idx, axis=0)

            data_low_emission = np.hstack([data_low_emission, data_roll])
            err_low_emission = np.hstack([err_low_emission, err_roll])

        shuffle_idx = np.random.permutation(data_low_emission.shape[1])
        data_shuffle = copy.deepcopy(data_low_emission[:, shuffle_idx])
        err_shuffle = copy.deepcopy(err_low_emission[:, shuffle_idx])

        eigen_system_dict = vw.run_robust_pca(data_shuffle.T,
                                              errors=err_shuffle.T,
                                              amount_of_eigen=self.pca_components,
                                              save_extra_param=False,
                                              number_of_iterations=3,
                                              c_sq=0.787 ** 2,
                                              )

        return eigen_system_dict

    def reconstruct_pca(self,
                        eigen_system_dict,
                        data,
                        err,
                        mask
                        ):

        mean_array = eigen_system_dict['m']
        eigen_vectors = eigen_system_dict['U']

        # fig, axs = plt.subplots(5, 1, sharex=True, figsize=(10, 8))
        # axs[0].plot(mean_array)
        #
        # for i in range(1, 5, 1):
        #     axs[i].plot(eigen_vectors[:, i - 1])
        #     # axs[i].set_ylim(-0.1, 0.3)

        # # plt.show()

        eigen_reconstruct = eigen_vectors[:, :self.pca_reconstruct_components]

        data[mask] = 0
        err[mask] = 0

        scores, norm = gappy.run_normgappy(err.T,
                                           data.T,
                                           mean_array,
                                           eigen_reconstruct,
                                           )
        noise_model = (scores @ eigen_reconstruct.T) + mean_array

        return noise_model

    def run_row_median(self,
                       ):
        """Calculate sigma-clipped median for each row. From Tom Williams."""

        zero_idx = np.where(self.hdu['SCI'].data == 0)
        self.hdu['SCI'].data[zero_idx] = np.nan

        mask = make_source_mask(self.hdu['SCI'].data,
                                nsigma=self.sigma,
                                npixels=self.npixels,
                                dilate_size=self.dilate_size,
                                )
        mask = mask | ~np.isfinite(self.hdu['SCI'].data)

        if self.quadrants:

            quadrant_size = int(self.hdu['SCI'].data.shape[1] / 4)

            quadrants = {}

            # Calculate medians and apply
            for i in range(4):
                quadrants[i] = {}

                quadrants[i]['data'] = self.hdu['SCI'].data[:, i * quadrant_size: (i + 1) * quadrant_size]
                quadrants[i]['mask'] = mask[:, i * quadrant_size: (i + 1) * quadrant_size]

                quadrants[i]['median'] = sigma_clipped_stats(quadrants[i]['data'],
                                                             mask=quadrants[i]['mask'],
                                                             sigma=self.sigma,
                                                             maxiters=self.max_iters,
                                                             axis=1,
                                                             )[1]

            # Apply this to the data
            for i in range(4):
                quadrants[i]['data_corr'] = \
                    (quadrants[i]['data'] - quadrants[i]['median'][:, np.newaxis]) + \
                    np.nanmedian(quadrants[i]['median'][:, np.newaxis])

            # Match quadrants in the overlaps
            for i in range(3):
                quadrants[i + 1]['data_corr'] += \
                    np.nanmedian(quadrants[i]['data_corr'][:, quadrant_size - 10:]) - \
                    np.nanmedian(quadrants[i + 1]['data_corr'][:, 0:10])

            # Reconstruct the data
            for i in range(4):
                self.hdu['SCI'].data[:, i * quadrant_size: (i + 1) * quadrant_size] = quadrants[i]['data_corr']

        else:

            median_arr = sigma_clipped_stats(self.hdu['SCI'].data,
                                             mask=mask,
                                             sigma=self.sigma,
                                             maxiters=self.max_iters,
                                             axis=1,
                                             )[1]

            self.hdu['SCI'].data -= median_arr[:, np.newaxis]

            # Bring everything back up to the median level
            self.hdu['SCI'].data += np.nanmedian(median_arr)

        self.hdu['SCI'].data[zero_idx] = 0

    def run_median_filter(self,
                          use_mask=True
                          ):
        """Run a series of filters over the row medians. From Mederic Boquien."""

        zero_idx = np.where(self.hdu['SCI'].data == 0)
        self.hdu['SCI'].data[zero_idx] = np.nan

        mask = None
        if use_mask:
            mask = make_source_mask(self.hdu['SCI'].data,
                                    nsigma=self.sigma,
                                    npixels=self.npixels,
                                    dilate_size=self.dilate_size,
                                    )
            mask = mask | ~np.isfinite(self.hdu['SCI'].data)

        if self.quadrants:

            quadrant_size = int(self.hdu['SCI'].data.shape[1] / 4)

            quadrants = {}

            # Calculate medians and apply
            for i in range(4):
                quadrants[i] = {}

                if use_mask:

                    data_quadrant = self.hdu['SCI'].data[:, i * quadrant_size: (i + 1) * quadrant_size]
                    mask_quadrant = mask[:, i * quadrant_size: (i + 1) * quadrant_size]

                    quadrants[i]['data'] = np.ma.array(data_quadrant, mask=mask_quadrant)
                else:
                    quadrants[i]['data'] = self.hdu['SCI'].data[:, i * quadrant_size: (i + 1) * quadrant_size]

            for i in range(4):

                for scale in self.median_filter_scales:

                    if use_mask:
                        med = np.ma.median(quadrants[i]['data'], axis=1)
                        mask_idx = np.where(med.mask)
                        med = med.data
                        med[mask_idx] = np.nan
                    else:
                        med = np.nanmedian(quadrants[i]['data'], axis=1)
                    nan_idx = np.where(~np.isfinite(med))

                    # We expect there to be 8 NaNs here, if there's more it's an issue
                    if len(nan_idx[0]) > 8:
                        raise Warning('Median filter failing on quadrants -- likely large extended source')

                    med[~np.isfinite(med)] = 0
                    noise = med - median_filter(med, scale)

                    if use_mask:
                        quadrants[i]['data'] = np.ma.array(quadrants[i]['data'].data - noise[:, np.newaxis],
                                                           mask=quadrants[i]['data'].mask)
                    else:
                        quadrants[i]['data'] -= noise[:, np.newaxis]

            for i in range(4):

                # Remove the mask since we don't need it now
                if use_mask:
                    quadrants[i]['data'] = quadrants[i]['data'].data

            # Match quadrants in the overlaps
            for i in range(3):
                quadrants[i + 1]['data'] += \
                    np.nanmedian(quadrants[i]['data'][:, quadrant_size - 10:]) - \
                    np.nanmedian(quadrants[i + 1]['data'][:, 0:10])

            # Reconstruct the data
            for i in range(4):
                self.hdu['SCI'].data[:, i * quadrant_size: (i + 1) * quadrant_size] = quadrants[i]['data']

        else:

            quadrant_size = self.hdu['SCI'].data.shape[1] // 4

            for scale in self.median_filter_scales:

                if use_mask:
                    data = np.ma.array(
                        copy.deepcopy(self.hdu['SCI'].data),
                        mask=copy.deepcopy(mask)
                    )
                else:
                    data = copy.deepcopy(self.hdu['SCI'].data)

                # Subtract off any lingering median quadrants
                quadrant_medians = []
                for i in range(4):

                    if use_mask:
                        quadrant_median = np.ma.median(data[:, i * quadrant_size: (i + 1) * quadrant_size])
                        quadrant_medians.append(quadrant_median)

                    else:
                        quadrant_median = np.nanpercentile(data[:, i * quadrant_size: (i + 1) * quadrant_size])
                        quadrant_medians.append(quadrant_median)

                    data[:, i * quadrant_size: (i + 1) * quadrant_size] -= quadrant_median

                if use_mask:
                    med = np.ma.median(data, axis=1)
                    mask_idx = np.where(med.mask)
                    med = med.data
                    med[mask_idx] = np.nan
                else:
                    med = np.nanmedian(data, axis=1)
                med[~np.isfinite(med)] = 0
                noise = med - median_filter(med, scale)

                self.hdu['SCI'].data -= noise[:, np.newaxis]

            self.hdu['SCI'].data = self.level_data(self.hdu['SCI'].data)

        self.hdu['SCI'].data[zero_idx] = 0

    def level_data(self,
                   data,
                   use_sigma_clipping=False,
                   ):
        """Level overlaps in NIRCAM amplifiers"""

        quadrant_size = data.shape[1] // 4

        mask = make_source_mask(data,
                                nsigma=self.sigma,
                                npixels=self.npixels,
                                dilate_size=self.dilate_size,
                                # dilate_size=1,
                                sigclip_iters=self.max_iters,
                                )

        for i in range(3):

            if use_sigma_clipping:
                med_1 = sigma_clipped_stats(data[:, i * quadrant_size: (i + 1) * quadrant_size],
                                            mask=mask[:, i * quadrant_size: (i + 1) * quadrant_size],
                                            sigma=self.sigma,
                                            maxiters=self.max_iters,
                                            )[1]

                med_2 = sigma_clipped_stats(data[:, (i + 1) * quadrant_size: (i + 2) * quadrant_size],
                                            mask=mask[:, (i + 1) * quadrant_size: (i + 2) * quadrant_size],
                                            sigma=self.sigma,
                                            maxiters=self.max_iters,
                                            )[1]

                data[:, (i + 1) * quadrant_size: (i + 2) * quadrant_size] += med_1 - med_2

            else:

                med_1 = np.nanmedian(data[:, i * quadrant_size: (i + 1) * quadrant_size][:, quadrant_size - 20:])
                med_2 = np.nanmedian(data[:, (i + 1) * quadrant_size: (i + 2) * quadrant_size][:, 0:20])

                data[:, (i + 1) * quadrant_size: (i + 2) * quadrant_size] += med_1 - med_2

        return data
