import copy
import gc
import glob
import logging
import multiprocessing as mp
import os
import pickle
import shutil
import warnings
from functools import partial

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from astropy.stats import sigma_clipped_stats
from mpl_toolkits.axes_grid1 import make_axes_locatable
from scipy.ndimage import median_filter
from scipy.stats import median_abs_deviation
from skimage import filters
from stdatamodels.jwst import datamodels
from tqdm import tqdm

from . import vwpca as vw
from . import vwpca_normgappy as gappy
from ..utils import make_source_mask, get_dq_bit_mask

matplotlib.use("agg")
log = logging.getLogger("stpipe")
log.addHandler(logging.NullHandler())

DESTRIPING_METHODS = [
    "row_median",
    "median_filter",
    "remstripe",
    "pca",
]


def get_dq_mask(data, err, dq):
    """Get good data quality, using bits and also 0/NaNs

    Args:
        data: Data array
        err: Error array
        dq: Data quality array
    """

    dq_mask = get_dq_bit_mask(dq)

    dq_mask = dq_mask | ~np.isfinite(data) | ~np.isfinite(err) | (data == 0)

    return dq_mask


def butterworth_filter(
    data,
    data_std=None,
    dq_mask=None,
    return_high_sn_mask=False,
):
    """Butterworth filter data, accounting for bad data

    Args:
        data: Data array
        data_std: Measured standard deviation of data. Defaults
            to None, which will measure using sigma-clipping
        dq_mask: User-supplied DQ mask. Defaults to None (no
            mask)
        return_high_sn_mask: Whether to separately return the
            high S/N mask or not. Defaults to False
    """

    data = copy.deepcopy(data)

    if data_std is None:
        data_std = sigma_clipped_stats(
            data,
            mask=dq_mask,
        )[2]

    # Make a high signal-to-noise mask, because these guys will cause bad ol' negative bowls
    high_sn_mask = make_source_mask(
        data,
        # nsigma=10,
        nsigma=5,
        npixels=1,
        dilate_size=1,
    )

    # Replace bad data with random noise
    idx = np.where(np.isnan(data) | dq_mask | high_sn_mask)
    data[idx] = np.random.normal(loc=0, scale=data_std, size=len(idx[0]))

    # Pad out the data by reflection to avoid ringing at boundaries
    data_pad = np.zeros([data.shape[0] * 2, data.shape[1] * 2])
    data_pad[: data.shape[0], : data.shape[1]] = copy.deepcopy(data)
    data_pad[-data.shape[0] :, -data.shape[1] :] = copy.deepcopy(data[::-1, ::-1])
    data_pad[-data.shape[0] :, : data.shape[1]] = copy.deepcopy(data[::-1, :])
    data_pad[: data.shape[0], -data.shape[1] :] = copy.deepcopy(data[:, ::-1])
    data_pad = np.roll(
        data_pad, axis=[0, 1], shift=[data.shape[0] // 2, data.shape[1] // 2]
    )
    data_pad = data_pad[
        data.shape[0] // 4 : -data.shape[0] // 4,
        data.shape[1] // 4 : -data.shape[1] // 4,
    ]

    # Filter the image to remove any large scale structure.
    data_filter = filters.butterworth(
        data_pad,
        high_pass=True,
    )
    data_filter = data_filter[
        data.shape[0] // 4 : -data.shape[0] // 4,
        data.shape[1] // 4 : -data.shape[1] // 4,
    ]
    # data_filter[idx] = np.random.normal(loc=0, scale=data_std, size=len(idx[0]))

    # Get rid of the high S/N stuff, replace with median
    data_filter[idx] = np.nan
    data_filter_med = np.nanmedian(data_filter, axis=1)
    for col in range(data_filter.shape[0]):
        col_idx = np.where(np.isnan(data_filter[col, :]))
        data_filter[col, col_idx[0]] = data_filter_med[col]

    if return_high_sn_mask:
        return data_filter, high_sn_mask
    else:
        return data_filter


class SingleTileDestripeStep:
    def __init__(
        self,
        in_dir,
        out_dir,
        step_ext,
        procs,
        quadrants=True,
        vertical_subtraction=True,
        destriping_method="row_median",
        filter_diffuse=False,
        large_scale_subtraction=False,
        sigma=3,
        npixels=3,
        dilate_size=11,
        max_iters=20,
        median_filter_scales=None,
        pca_components=50,
        pca_reconstruct_components=10,
        overwrite=False,
    ):
        """NIRCAM Destriping routines

        Contains a number of routines to destripe NIRCAM data -- median filtering, PCA, and an equivalent of
        remstripe from the CEERS team

        Args:
            in_dir: Input directory
            out_dir: Output directory
            step_ext: .fits file extension to run step on
            procs: Number of processes to run in parallel
            quadrants: Whether to split the chip into 512 pixel segments, and destripe each (mostly)
                separately. Defaults to True
            vertical_subtraction: Perform sigma-clipped median column subtraction? Defaults to True
            destriping_method: Method to use for destriping. Allowed options are given by DESTRIPING_METHODS
            filter_diffuse: Whether to perform high-pass filter on data, to remove diffuse, extended
                emission. Defaults to False, but should be set True for observations where emission fills the FOV
            large_scale_subtraction: Whether to mitigate for large-scale stripes remaining after the diffuse
                filtering. Defaults to False
            sigma: Sigma for sigma-clipping. Defaults to 3
            npixels: Pixels to grow for masking. Defaults to 5
            dilate_size: make_source_mask dilation size. Defaults to 11
            max_iters: Maximum sigma-clipping iterations. Defaults to 20
            median_filter_scales: Scales for median filtering
            pca_components: Number of PCA components to model. Defaults to 50
            pca_reconstruct_components: Number of PCA components to use in reconstruction. Defaults to 10
            overwrite: Whether to overwrite or not. Defaults to False
        """

        if destriping_method not in DESTRIPING_METHODS:
            raise Warning(
                f"destriping_method should be one of {DESTRIPING_METHODS}, not {destriping_method}"
            )

        if median_filter_scales is None:
            median_filter_scales = [3, 7, 15, 31, 63, 127]

        self.in_dir = in_dir
        self.out_dir = out_dir
        self.step_ext = step_ext
        self.procs = procs
        self.plot_dir = os.path.join(
            self.out_dir,
            "plots",
        )

        self.quadrants = quadrants
        self.vertical_subtraction = vertical_subtraction
        self.destriping_method = destriping_method
        self.filter_diffuse = filter_diffuse
        self.large_scale_subtraction = large_scale_subtraction
        self.sigma = sigma
        self.npixels = npixels
        self.dilate_size = dilate_size
        self.max_iters = max_iters
        self.median_filter_scales = median_filter_scales
        self.pca_components = pca_components
        self.pca_reconstruct_components = pca_reconstruct_components
        self.overwrite = overwrite

    def do_step(self):
        """Run single-tile destriping"""

        if self.overwrite:
            shutil.rmtree(self.out_dir)

        if not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)

        if not os.path.exists(self.plot_dir):
            os.makedirs(self.plot_dir)

        # Check if we've already run the step
        step_complete_file = os.path.join(
            self.out_dir,
            "single_tile_destripe_step_complete.txt",
        )
        if os.path.exists(step_complete_file):
            log.info("Step already run")
            return True

        files = glob.glob(
            os.path.join(
                self.in_dir,
                f"*_{self.step_ext}.fits",
            )
        )
        files.sort()

        # Ensure we're not wasting processes
        procs = np.nanmin([self.procs, len(files)])

        successes = self.run_step(
            files,
            procs=procs,
        )

        # If not everything has succeeded, then return a warning
        if not np.all(successes):
            log.warning("Failures detected in destriping")
            return False

        with open(step_complete_file, "w+") as f:
            f.close()

        return True

    def run_step(
        self,
        files,
        procs=1,
    ):
        """Wrap paralellism around the destriping

        Args:
            files: List of files to destripe
            procs: Number of parallel processes to run.
                Defaults to 1
        """

        log.info("Beginning destriping")

        with mp.get_context("fork").Pool(procs) as pool:
            successes = []

            for success in tqdm(
                pool.imap_unordered(
                    partial(
                        self.parallel_destripe,
                    ),
                    files,
                ),
                ascii=True,
                desc="Destriping",
                total=len(files),
            ):
                successes.append(success)

            pool.close()
            pool.join()
            gc.collect()

        return successes

    def parallel_destripe(
        self,
        file,
    ):
        """Parallel destriping function

        Args:
            file: input file
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            im = datamodels.open(file)

            short_name = os.path.split(file)[-1]
            out_name = os.path.join(
                self.out_dir,
                short_name,
            )

            # Check if this is a subarray
            is_subarray = "sub" in im.meta.subarray.name.lower()

            quadrants = copy.deepcopy(self.quadrants)
            if is_subarray:
                # Force off quadrants if we're in subarray mode
                quadrants = False

            if quadrants:
                im.data = self.level_data(im)

            full_noise_model = np.zeros_like(im.data)

            # Do vertical subtraction, if requested
            if self.vertical_subtraction:
                full_noise_model += self.run_vertical_subtraction(
                    im=im,
                    prev_noise_model=full_noise_model,
                    is_subarray=is_subarray,
                )

            if self.destriping_method == "row_median":
                full_noise_model += self.run_row_median(
                    im=im,
                    prev_noise_model=full_noise_model,
                    out_name=out_name,
                    quadrants=quadrants,
                )
            elif self.destriping_method == "median_filter":
                full_noise_model += self.run_median_filter(
                    im=im,
                    prev_noise_model=full_noise_model,
                    out_name=out_name,
                    quadrants=quadrants,
                )
            elif self.destriping_method == "remstripe":
                full_noise_model += self.run_remstriping(
                    im=im,
                    prev_noise_model=full_noise_model,
                    out_name=out_name,
                    is_subarray=is_subarray,
                    quadrants=quadrants,
                )
            elif self.destriping_method == "pca":
                pca_dir = os.path.join(
                    self.out_dir,
                    "pca",
                )
                if not os.path.exists(pca_dir):
                    os.makedirs(pca_dir)
                pca_file = os.path.join(
                    pca_dir,
                    short_name.replace(".fits", ".pkl"),
                )

                full_noise_model += self.run_pca_denoise(
                    im=im,
                    prev_noise_model=full_noise_model,
                    pca_file=pca_file,
                    out_name=out_name,
                    is_subarray=is_subarray,
                    quadrants=quadrants,
                )
            else:
                raise NotImplementedError(
                    f"Destriping method {self.destriping_method} not implemented"
                )

            zero_idx = np.where(im.data == 0)
            nan_idx = np.where(np.isnan(im.data))

            im.data -= full_noise_model

            im.data[zero_idx] = 0
            im.data[nan_idx] = np.nan

            if self.plot_dir is not None:
                self.make_destripe_plot(
                    in_im=im,
                    noise_model=full_noise_model,
                    out_name=out_name,
                )

            im.save(out_name)

            del im

            return True

    def run_vertical_subtraction(
        self,
        im,
        prev_noise_model,
        is_subarray=False,
    ):
        """Median filter subtraction of columns (optional diffuse emission filtering)

        Args:
            im: Input datamodel
            prev_noise_model: Already calculated noise model, to subtract before
                doing vertical subtraction
            is_subarray: Whether the image is a subarray. Defaults to False
        """

        data = copy.deepcopy(im.data)
        full_noise_model = np.zeros_like(data)

        zero_idx = np.where(data == 0)

        data[zero_idx] = np.nan

        data -= prev_noise_model

        mask = make_source_mask(
            data,
            nsigma=self.sigma,
            npixels=self.npixels,
            dilate_size=self.dilate_size,
            sigclip_iters=self.max_iters,
        )

        dq_mask = get_dq_mask(
            data=data,
            err=im.err,
            dq=im.dq,
        )

        mask = mask | dq_mask

        # Cut out the reference edge pixels if we're not in subarray mode
        if not is_subarray:
            data = copy.deepcopy(data[4:-4, 4:-4])
            mask = mask[4:-4, 4:-4]
            dq_mask = dq_mask[4:-4, 4:-4]
        else:
            data = copy.deepcopy(data)

        vertical_noise_model = np.zeros_like(data)

        if self.filter_diffuse:
            data, mask = self.get_filter_diffuse(
                data=data,
                mask=mask,
                dq_mask=dq_mask,
            )

        # Use median filtering to avoid noise and boundary issues
        data = np.ma.array(copy.deepcopy(data), mask=copy.deepcopy(mask))

        for scale in self.median_filter_scales:
            med = np.ma.median(data, axis=0)
            mask_idx = np.where(med.mask)
            med = med.data
            med[mask_idx] = np.nan
            med[~np.isfinite(med)] = 0
            noise = med - median_filter(med, scale, mode="reflect")

            data -= noise[np.newaxis, :]

            vertical_noise_model += noise[np.newaxis, :]

        # Bring everything back up to the median level
        vertical_noise_model -= np.nanmedian(vertical_noise_model)

        if not is_subarray:
            full_noise_model[4:-4, 4:-4] += copy.deepcopy(vertical_noise_model)
        else:
            full_noise_model += copy.deepcopy(vertical_noise_model)

        return full_noise_model

    def run_remstriping(
        self,
        im,
        prev_noise_model,
        out_name,
        is_subarray=False,
        quadrants=True,
    ):
        """Destriping based on the CEERS remstripe routine

        Mask out sources, then collapse a median along x and y to remove stripes.

        Args:
            im: Input datamodel
            prev_noise_model: Previously calculated noise model, to subtract before
                destriping
            out_name: Output filename
            is_subarray: Whether image is subarray or not. Defaults to False
            quadrants: Whether to break out by quadrants. Defaults to True
        """

        im_data = copy.deepcopy(im.data)

        zero_idx = np.where(im_data == 0)

        im_data[zero_idx] = np.nan

        im_data -= prev_noise_model

        quadrant_size = im_data.shape[1] // 4

        mask = make_source_mask(
            im_data,
            nsigma=self.sigma,
            npixels=self.npixels,
            dilate_size=self.dilate_size,
            sigclip_iters=self.max_iters,
        )

        dq_mask = get_dq_mask(
            im_data,
            im.err,
            im.dq,
        )

        mask = mask | dq_mask

        # Cut out the reference edge pixels if we're not in subarray mode
        if not is_subarray:
            data = copy.deepcopy(im_data[4:-4, 4:-4])
            mask = mask[4:-4, 4:-4]
            dq_mask = dq_mask[4:-4, 4:-4]
        else:
            data = copy.deepcopy(im_data)

        if self.filter_diffuse:
            data, mask = self.get_filter_diffuse(
                data=data,
                mask=mask,
                dq_mask=dq_mask,
            )

        full_noise_model = np.zeros_like(im_data)
        trimmed_noise_model = np.zeros_like(data)

        if quadrants:
            # Calculate medians and apply
            for i in range(4):
                if not is_subarray:
                    if i == 0:
                        idx_slice = slice(0, quadrant_size - 4)
                    elif i == 3:
                        idx_slice = slice(1532, 2040)
                    else:
                        idx_slice = slice(
                            i * quadrant_size - 4, (i + 1) * quadrant_size - 4
                        )
                else:
                    idx_slice = slice(i * quadrant_size, (i + 1) * quadrant_size)

                data_quadrants = data[:, idx_slice]
                mask_quadrants = mask[:, idx_slice]

                # Collapse first along the y direction
                median_quadrants = sigma_clipped_stats(
                    data_quadrants,
                    mask=mask_quadrants,
                    sigma=self.sigma,
                    maxiters=self.max_iters,
                    axis=1,
                )[1]

                # Subtract this off the data, then collapse along x direction

                x_stripes = median_quadrants[:, np.newaxis] - np.nanmedian(
                    median_quadrants
                )

                trimmed_noise_model[:, idx_slice] += x_stripes
                data_quadrants_1 = data_quadrants - x_stripes

                median_quadrants = sigma_clipped_stats(
                    data_quadrants_1,
                    mask=mask_quadrants,
                    sigma=self.sigma,
                    maxiters=self.max_iters,
                    axis=0,
                )[1]

                y_stripes = median_quadrants[np.newaxis, :] - np.nanmedian(
                    median_quadrants
                )

                trimmed_noise_model[:, idx_slice] += y_stripes

        else:
            median_arr = sigma_clipped_stats(
                data,
                mask=mask,
                sigma=self.sigma,
                maxiters=self.max_iters,
                axis=1,
            )[1]

            trimmed_noise_model += median_arr[:, np.newaxis]

            # Bring everything back up to the median level
            trimmed_noise_model -= np.nanmedian(median_arr)

        if self.plot_dir is not None and mask is not None:
            self.make_mask_plot(
                data=data,
                mask=mask,
                out_name=out_name,
                filter_diffuse=self.filter_diffuse,
            )

        if not is_subarray:
            full_noise_model[4:-4, 4:-4] = copy.deepcopy(trimmed_noise_model)
        else:
            full_noise_model = copy.deepcopy(trimmed_noise_model)

        return full_noise_model

    def run_pca_denoise(
        self,
        im,
        prev_noise_model,
        pca_file,
        out_name,
        is_subarray=False,
        quadrants=True,
    ):
        """PCA-based de-noising

        Build a PCA model for the noise using the robust PCA implementation from Tamas Budavari and Vivienne Wild. We
        mask the data, optionally high-pass filter (Butterworth) to remove extended diffuse emission, and build the PCA
        model from there. pca_final_med_row_subtraction is on, it will do a final row-by-row median subtraction, to
        catch large-scale noise that might get filtered out.

        Args:
            im: Input datamodel
            prev_noise_model: Previously calculated noise model, to subtract before
                destriping
            pca_file: Where to save PCA model to
            out_name: Output filename
            is_subarray: Whether image is subarray or not. Defaults to False
            quadrants: Whether to break out by quadrants. Defaults to True
        """

        im_data = copy.deepcopy(im.data)

        zero_idx = np.where(im_data == 0)

        quadrant_size = im_data.shape[1] // 4

        im_data[zero_idx] = np.nan

        im_data -= prev_noise_model

        mask = make_source_mask(
            im_data,
            nsigma=self.sigma,
            npixels=self.npixels,
            dilate_size=self.dilate_size,
            sigclip_iters=self.max_iters,
        )

        dq_mask = get_dq_mask(data=im_data, err=im.err, dq=im.dq)

        mask = mask | dq_mask

        data = copy.deepcopy(im_data)
        err = copy.deepcopy(im.err)
        original_mask = copy.deepcopy(mask)

        # Trim off the 0 rows/cols if we're using the full array

        if not is_subarray:
            data = data[4:-4, 4:-4]
            err = err[4:-4, 4:-4]
            dq_mask = dq_mask[4:-4, 4:-4]
            mask = mask[4:-4, 4:-4]

        data_mean, data_med, data_std = sigma_clipped_stats(
            data,
            mask=mask,
            sigma=self.sigma,
            maxiters=self.max_iters,
        )

        data -= data_med

        if self.filter_diffuse:
            data_train, mask_train = self.get_filter_diffuse(
                data=data,
                mask=mask,
                dq_mask=dq_mask,
            )

        else:
            data_train = copy.deepcopy(data)
            mask_train = copy.deepcopy(mask)

        if out_name:
            self.make_mask_plot(
                data=data_train,
                mask=mask_train,
                out_name=out_name,
                filter_diffuse=self.filter_diffuse,
            )

        if quadrants:
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
                    idx_slice = slice(
                        i * quadrant_size - 4, (i + 1) * quadrant_size - 4
                    )

                data_quadrant = copy.deepcopy(data_train[:, idx_slice])
                train_mask_quadrant = copy.deepcopy(mask_train[:, idx_slice])
                err_quadrant = copy.deepcopy(err[:, idx_slice])

                norm_factor = np.abs(
                    np.diff(np.nanpercentile(data_quadrant, [16, 84]))[0]
                )
                norm_median = np.nanmedian(data_quadrant)

                data_quadrant = (data_quadrant - norm_median) / norm_factor + 1
                err_quadrant /= norm_factor

                # Get NaNs out of the error map
                quadrant_nan_idx = np.where(np.isnan(err_quadrant))
                data_quadrant[quadrant_nan_idx] = np.nan
                err_quadrant[quadrant_nan_idx] = 0

                # Replace NaNd data with column median
                for col in range(data_quadrant.shape[0]):
                    idx = np.where(np.isnan(data_quadrant[col, :]))
                    data_quadrant[col, idx[0]] = (
                        data_med[col] - norm_median
                    ) / norm_factor + 1

                # For places where this is all NaN, just 0 to avoid errors
                data_quadrant[np.isnan(data_quadrant)] = 0

                # data_quadrant[train_mask_quadrant] = 0
                err_quadrant[train_mask_quadrant] = 0

                if pca_file is not None:
                    indiv_pca_file = pca_file.replace(".pkl", f"_amp_{i}.pkl")
                else:
                    indiv_pca_file = None

                if indiv_pca_file is not None and os.path.exists(pca_file):
                    with open(pca_file, "rb") as f:
                        eigen_system_dict = pickle.load(f)
                else:
                    eigen_system_dict = self.fit_robust_pca(
                        data_quadrant,
                        err_quadrant,
                        train_mask_quadrant,
                    )
                    if indiv_pca_file is not None:
                        with open(indiv_pca_file, "wb") as f:
                            pickle.dump(eigen_system_dict, f)

                noise_model = self.reconstruct_pca(
                    eigen_system_dict, data_quadrant, err_quadrant, train_mask_quadrant
                )

                noise_model = (noise_model.T - 1) * norm_factor + norm_median
                noise_model_arr[:, idx_slice] = copy.deepcopy(noise_model)

            full_noise_model = np.full_like(im_data, np.nan)
            full_noise_model[4:-4, 4:-4] = copy.deepcopy(noise_model_arr)

        else:
            data_train[mask_train] = np.nan
            err_train = copy.deepcopy(err)

            # Remove NaNs
            train_nan_idx = np.where(np.isnan(err_train))
            data_train[train_nan_idx] = np.nan
            err_train[train_nan_idx] = 0

            data_med = np.nanmedian(data_train, axis=1)

            norm_median = np.nanmedian(data_train)
            norm_factor = median_abs_deviation(data_train, axis=None, nan_policy="omit")

            data_train = (data_train - norm_median) / norm_factor + 1
            err_train /= norm_factor

            # Replace NaNd data with column median
            for col in range(data_train.shape[0]):
                idx = np.where(np.isnan(data_train[col, :]))
                data_train[col, idx[0]] = (
                    data_med[col] - norm_median
                ) / norm_factor + 1

            # For places where this is all NaN, just 0 to avoid errors
            data_train[np.isnan(data_train)] = 0

            # data_train[mask_train] = 0
            err_train[mask_train] = 0

            if pca_file is not None and os.path.exists(pca_file):
                with open(pca_file, "rb") as f:
                    eigen_system_dict = pickle.load(f)
            else:
                eigen_system_dict = self.fit_robust_pca(
                    data_train,
                    err_train,
                    mask_train,
                )
                if pca_file is not None:
                    with open(pca_file, "wb") as f:
                        pickle.dump(eigen_system_dict, f)

            noise_model = self.reconstruct_pca(
                eigen_system_dict, data_train, err_train, mask_train
            )

            noise_model = (noise_model.T - 1) * norm_factor

            full_noise_model = np.full_like(im_data, np.nan)

            if is_subarray:
                full_noise_model = copy.deepcopy(noise_model)
            else:
                full_noise_model[4:-4, 4:-4] = copy.deepcopy(noise_model)

        # Centre the noise model around 0 to preserve flux
        noise_med = sigma_clipped_stats(
            full_noise_model,
            mask=original_mask,
            sigma=self.sigma,
            maxiters=self.max_iters,
        )[1]
        full_noise_model -= noise_med

        return full_noise_model

    def fit_robust_pca(
        self,
        data,
        err,
        mask,
        mask_column_frac=0.25,
        min_column_frac=0.5,
    ):
        """Fits the robust PCA algorithm

        Args:
            data: Input data
            err: Input errors
            mask: Where data is masked
            mask_column_frac: In low masked cases, take the data where less than
                mask_column_frac is masked. Defaults to 0.25
            min_column_frac: In highly masked cases, take
                min_column_frac of data to ensure we have enough to fit. Defaults
                to 0.5
        """
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

        eigen_system_dict = vw.run_robust_pca(
            data_shuffle.T,
            errors=err_shuffle.T,
            amount_of_eigen=self.pca_components,
            save_extra_param=False,
            number_of_iterations=3,
            c_sq=0.787**2,
        )

        return eigen_system_dict

    def reconstruct_pca(self, eigen_system_dict, data, err, mask):
        """Reconstruct PCA from the fit

        Args:
            eigen_system_dict: Dictionary of the outputs from the PCA
                fit
            data: Input data
            err: Input error
            mask: Input mask
        """
        mean_array = eigen_system_dict["m"]
        eigen_vectors = eigen_system_dict["U"]

        eigen_reconstruct = eigen_vectors[:, : self.pca_reconstruct_components]

        data[mask] = 0
        err[mask] = 0

        scores, norm = gappy.run_normgappy(
            err.T,
            data.T,
            mean_array,
            eigen_reconstruct,
        )
        noise_model = (scores @ eigen_reconstruct.T) + mean_array

        return noise_model

    def run_row_median(
        self,
        im,
        out_name,
        prev_noise_model,
        quadrants=True,
    ):
        """Calculate sigma-clipped median for each row. From Tom Williams.

        Args:
            im: Input datamodel
            out_name: Output filename
            prev_noise_model: Previously calculated noise model, to subtract before
                destriping
            out_name: Output filename
            quadrants: Whether to break out by quadrants. Defaults to True
        """

        im_data = copy.deepcopy(im.data)

        zero_idx = np.where(im_data == 0)
        im_data[zero_idx] = np.nan

        im_data -= prev_noise_model

        mask = make_source_mask(
            im_data,
            nsigma=self.sigma,
            npixels=self.npixels,
            dilate_size=self.dilate_size,
        )

        dq_mask = get_dq_mask(
            data=im_data,
            err=im.err,
            dq=im.dq,
        )

        mask = mask | dq_mask

        full_noise_model = np.zeros_like(im_data)

        if self.filter_diffuse:
            data, mask = self.get_filter_diffuse(
                data=im_data,
                mask=mask,
                dq_mask=dq_mask,
            )
        else:
            data = copy.deepcopy(im_data)

        if quadrants:
            quadrant_size = int(data.shape[1] / 4)

            # Calculate medians and apply
            for i in range(4):
                data_quadrants = data[:, i * quadrant_size : (i + 1) * quadrant_size]
                mask_quadrants = mask[:, i * quadrant_size : (i + 1) * quadrant_size]

                median_quadrants = sigma_clipped_stats(
                    data_quadrants,
                    mask=mask_quadrants,
                    sigma=self.sigma,
                    maxiters=self.max_iters,
                    axis=1,
                )[1]

                full_noise_model[
                    :, i * quadrant_size : (i + 1) * quadrant_size
                ] += median_quadrants[:, np.newaxis]
                full_noise_model[
                    :, i * quadrant_size : (i + 1) * quadrant_size
                ] -= np.nanmedian(median_quadrants)

        else:
            median_arr = sigma_clipped_stats(
                data,
                mask=mask,
                sigma=self.sigma,
                maxiters=self.max_iters,
                axis=1,
            )[1]

            full_noise_model += median_arr[:, np.newaxis]

            # Bring everything back up to the median level
            full_noise_model -= np.nanmedian(median_arr)

        if out_name is not None:
            self.make_mask_plot(
                data=data,
                mask=mask,
                out_name=out_name,
                filter_diffuse=self.filter_diffuse,
            )

        return full_noise_model

    def run_median_filter(
        self,
        im,
        prev_noise_model,
        out_name,
        use_mask=True,
        quadrants=True,
    ):
        """Run a series of filters over the row medians. From Mederic Boquien.

        Args:
            im: Input datamodel
            prev_noise_model: Previously calculated noise model, to subtract before
                destriping
            out_name: Output filename
            use_mask: Whether to mask data or not. Defaults to True
            quadrants: Whether to break out by quadrants. Defaults to True
        """

        im_data = copy.deepcopy(im.data)

        zero_idx = np.where(im_data == 0)
        im_data[zero_idx] = np.nan

        im_data -= prev_noise_model

        full_noise_model = np.zeros_like(im_data)

        mask = None
        dq_mask = None
        if use_mask:
            mask = make_source_mask(
                im_data,
                nsigma=self.sigma,
                npixels=self.npixels,
                dilate_size=self.dilate_size,
            )
            dq_mask = get_dq_mask(
                data=im_data,
                err=im.err,
                dq=im.dq,
            )
            mask = mask | dq_mask

        if self.filter_diffuse:
            data, mask = self.get_filter_diffuse(
                data=im_data,
                mask=mask,
                dq_mask=dq_mask,
            )
        else:
            data = copy.deepcopy(im_data)

        if quadrants:
            quadrant_size = int(data.shape[1] / 4)

            # Calculate medians and apply
            for i in range(4):
                if use_mask:
                    data_quadrant = data[:, i * quadrant_size : (i + 1) * quadrant_size]
                    mask_quadrant = mask[:, i * quadrant_size : (i + 1) * quadrant_size]

                    data_quadrant = np.ma.array(data_quadrant, mask=mask_quadrant)
                else:
                    data_quadrant = data[:, i * quadrant_size : (i + 1) * quadrant_size]

                for scale in self.median_filter_scales:
                    if use_mask:
                        med = np.ma.median(data_quadrant, axis=1)
                        mask_idx = np.where(med.mask)
                        med = med.data
                        med[mask_idx] = np.nan
                    else:
                        med = np.nanmedian(data_quadrant, axis=1)

                    # Replace any remaining NaNs with the median
                    med[~np.isfinite(med)] = np.nanmedian(med)
                    noise = med - median_filter(med, scale, mode="reflect")

                    if use_mask:
                        data_quadrant = np.ma.array(
                            data_quadrant.data - noise[:, np.newaxis],
                            mask=data_quadrant.mask,
                        )
                    else:
                        data_quadrant -= noise[:, np.newaxis]

                    full_noise_model[
                        :, i * quadrant_size : (i + 1) * quadrant_size
                    ] += noise[:, np.newaxis]

        else:
            if use_mask:
                data = np.ma.array(copy.deepcopy(data), mask=copy.deepcopy(mask))
            else:
                data = copy.deepcopy(data)

            for scale in self.median_filter_scales:
                if use_mask:
                    med = np.ma.median(data, axis=1)
                    mask_idx = np.where(med.mask)
                    med = med.data
                    med[mask_idx] = np.nan
                else:
                    med = np.nanmedian(data, axis=1)
                med[~np.isfinite(med)] = 0
                noise = med - median_filter(med, scale, mode="reflect")

                data -= noise[:, np.newaxis]

                full_noise_model += noise[:, np.newaxis]

        if out_name is not None:
            self.make_mask_plot(
                data=data,
                mask=mask,
                out_name=out_name,
                filter_diffuse=self.filter_diffuse,
            )

        return full_noise_model

    def level_data(
        self,
        im,
    ):
        """Level overlaps in NIRCAM amplifiers

        Args:
            im: Input datamodel
        """

        data = copy.deepcopy(im.data)

        quadrant_size = data.shape[1] // 4

        dq_mask = get_dq_mask(
            data=im.data,
            err=im.err,
            dq=im.dq,
        )

        for i in range(3):
            quad_1 = data[:, i * quadrant_size : (i + 1) * quadrant_size][
                :, quadrant_size - 20 :
            ]
            dq_1 = dq_mask[:, i * quadrant_size : (i + 1) * quadrant_size][
                :, quadrant_size - 20 :
            ]
            quad_2 = data[:, (i + 1) * quadrant_size : (i + 2) * quadrant_size][:, :20]
            dq_2 = dq_mask[:, (i + 1) * quadrant_size : (i + 2) * quadrant_size][:, :20]

            quad_1[dq_1] = np.nan
            quad_2[dq_2] = np.nan

            med_1 = np.nanmedian(
                quad_1,
                axis=1,
            )
            med_2 = np.nanmedian(
                quad_2,
                axis=1,
            )
            diff = med_1 - med_2

            delta = sigma_clipped_stats(diff, maxiters=None)[1]
            data[:, (i + 1) * quadrant_size : (i + 2) * quadrant_size] += delta

        return data

    def get_filter_diffuse(
        self,
        data,
        mask,
        dq_mask,
    ):
        """Filter out diffuse emission using Butterworth filter

        Args:
            data: Input data
            mask: Pre-calculated mask
            dq_mask: Calculated data quality mask
        """

        data_mean, data_med, data_std = sigma_clipped_stats(
            data,
            mask=mask,
            sigma=self.sigma,
            maxiters=self.max_iters,
        )

        data_filter, high_sn_mask = butterworth_filter(
            data, data_std=data_std, dq_mask=dq_mask, return_high_sn_mask=True
        )
        data = copy.deepcopy(data_filter)

        # Make a mask from this data. Create a positive mask
        # with a relatively strict size tolerance, to make sure
        # we don't just get random noise

        mask_pos = make_source_mask(
            data,
            mask=dq_mask,
            nsigma=self.sigma,
            npixels=10,
            dilate_size=self.dilate_size,
            sigclip_iters=self.max_iters,
        )

        # Create a negative mask with relatively strong
        # dilation, to make sure we catch all those negs
        mask_neg = make_source_mask(
            -data,
            mask=dq_mask | mask_pos,
            nsigma=self.sigma,
            npixels=self.npixels,
            sigclip_iters=self.max_iters,
        )

        mask = mask_pos | mask_neg | dq_mask

        return data, mask

    def make_mask_plot(
        self,
        data,
        mask,
        out_name,
        filter_diffuse=False,
    ):
        """Create mask diagnostic plot

        Args:
            data: Input data
            mask: Calculated mask
            out_name: Output filename
            filter_diffuse: Whether to filter diffuse emission. Defaults to False
        """

        plot_name = os.path.join(
            self.plot_dir,
            out_name.split(os.path.sep)[-1].replace(".fits", "_filter+mask"),
        )

        vmin, vmax = np.nanpercentile(data, [2, 98])
        plt.figure(figsize=(8, 4))
        plt.subplot(1, 2, 1)
        plt.imshow(
            data,
            origin="lower",
            vmin=vmin,
            vmax=vmax,
            interpolation="nearest",
        )

        plt.axis("off")

        if filter_diffuse:
            title = "Filtered Data"
        else:
            title = "Data"

        plt.title(title)

        plt.subplot(1, 2, 2)
        plt.imshow(
            mask,
            origin="lower",
            interpolation="nearest",
        )

        plt.axis("off")

        plt.title("Mask")

        plt.savefig(f"{plot_name}.png", bbox_inches="tight")
        plt.savefig(f"{plot_name}.pdf", bbox_inches="tight")
        plt.close()

    def make_destripe_plot(
        self,
        in_im,
        noise_model,
        out_name,
    ):
        """Create diagnostic plot for the destriping

        Args:
            in_im: Input datamodel
            noise_model: Model for the stripes
            out_name: Output filename
        """

        data = copy.deepcopy(in_im.data)

        nan_idx = np.where(np.isnan(data))
        zero_idx = np.where(data == 0)
        original_data = data + noise_model
        original_data[zero_idx] = 0
        original_data[nan_idx] = np.nan

        plot_name = os.path.join(
            self.plot_dir,
            out_name.split(os.path.sep)[-1].replace(".fits", "_noise_model"),
        )

        vmin, vmax = np.nanpercentile(noise_model, [1, 99])
        vmin_data, vmax_data = np.nanpercentile(data, [10, 90])

        plt.figure(figsize=(8, 4))

        ax = plt.subplot(1, 3, 1)
        im = plt.imshow(
            original_data,
            origin="lower",
            vmin=vmin_data,
            vmax=vmax_data,
            interpolation="nearest",
        )
        plt.axis("off")

        plt.title("Original Data")

        divider = make_axes_locatable(ax)
        cax = divider.append_axes("bottom", size="5%", pad=0)

        plt.colorbar(im, cax=cax, label="MJy/sr", orientation="horizontal")

        ax = plt.subplot(1, 3, 2)
        im = plt.imshow(
            noise_model,
            origin="lower",
            vmin=vmin,
            vmax=vmax,
            interpolation="nearest",
        )
        plt.axis("off")

        plt.title("Noise Model")

        divider = make_axes_locatable(ax)
        cax = divider.append_axes("bottom", size="5%", pad=0)

        plt.colorbar(im, cax=cax, label="MJy/sr", orientation="horizontal")

        ax = plt.subplot(1, 3, 3)
        im = plt.imshow(
            data,
            origin="lower",
            vmin=vmin_data,
            vmax=vmax_data,
            interpolation="nearest",
        )
        plt.axis("off")

        plt.title("Destriped Data")

        divider = make_axes_locatable(ax)
        cax = divider.append_axes("bottom", size="5%", pad=0)

        plt.colorbar(im, cax=cax, label="MJy/sr", orientation="horizontal")

        plt.subplots_adjust(hspace=0, wspace=0)

        plt.savefig(f"{plot_name}.png", bbox_inches="tight")
        plt.savefig(f"{plot_name}.pdf", bbox_inches="tight")
        plt.close()

        return True