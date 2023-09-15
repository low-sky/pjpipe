import copy
import gc
import glob
import logging
import multiprocessing as mp
import os
import shutil
import warnings
from functools import partial

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from astropy.stats import sigma_clipped_stats
from mpl_toolkits.axes_grid1 import make_axes_locatable
from reproject import reproject_interp
from reproject.mosaicking import find_optimal_celestial_wcs
from scipy.ndimage import uniform_filter1d
from stdatamodels.jwst import datamodels
from tqdm import tqdm

from ..utils import get_dq_bit_mask, make_source_mask, reproject_image

matplotlib.use("agg")
log = logging.getLogger("stpipe")
log.addHandler(logging.NullHandler())

ALLOWED_WEIGHT_TYPES = ["exptime", "ivm"]

# Global variables, to speed up multiprocessing
data_reproj = []
weight_reproj = []


def make_diagnostic_plot(
    plot_name,
    data,
    stripes,
    figsize=(9, 4),
):
    """Make a diagnostic plot to show the destriping

    Args:
        plot_name: Output name for plot
        data: Original data
        stripes: Stripe model
        figsize: Size for the figure. Defaults to (9, 4)
    """

    plt.figure(figsize=figsize)

    n_rows = 1
    n_cols = 3

    vmin_data, vmax_data = np.nanpercentile(data, [10, 90])
    vmin_stripes, vmax_stripes = np.nanpercentile(stripes, [1, 99])

    # Plot the uncorrected data
    ax = plt.subplot(n_rows, n_cols, 1)

    im = plt.imshow(
        data,
        origin="lower",
        interpolation="nearest",
        vmin=vmin_data,
        vmax=vmax_data,
    )
    plt.axis("off")

    plt.text(
        0.05,
        0.95,
        "Uncorr. data",
        ha="left",
        va="top",
        fontweight="bold",
        bbox=dict(facecolor="white", edgecolor="black", alpha=0.7),
        transform=ax.transAxes,
    )

    divider = make_axes_locatable(ax)
    cax = divider.append_axes("bottom", size="5%", pad=0)

    plt.colorbar(im, cax=cax, label="MJy/sr", orientation="horizontal")

    # Plot the stripes model
    ax = plt.subplot(n_rows, n_cols, 2)

    im = plt.imshow(
        stripes,
        origin="lower",
        interpolation="nearest",
        vmin=vmin_stripes,
        vmax=vmax_stripes,
    )
    plt.axis("off")

    plt.text(
        0.05,
        0.95,
        "Noise model",
        ha="left",
        va="top",
        fontweight="bold",
        bbox=dict(facecolor="white", edgecolor="black", alpha=0.7),
        transform=ax.transAxes,
    )

    divider = make_axes_locatable(ax)
    cax = divider.append_axes("bottom", size="5%", pad=0)

    plt.colorbar(im, cax=cax, label="MJy/sr", orientation="horizontal")

    # And finally, the corrected data
    ax = plt.subplot(n_rows, n_cols, 3)

    im = plt.imshow(
        data - stripes,
        origin="lower",
        interpolation="nearest",
        vmin=vmin_data,
        vmax=vmax_data,
    )
    plt.axis("off")

    plt.text(
        0.05,
        0.95,
        "Corr. data",
        ha="left",
        va="top",
        fontweight="bold",
        bbox=dict(facecolor="white", edgecolor="black", alpha=0.7),
        transform=ax.transAxes,
    )

    divider = make_axes_locatable(ax)
    cax = divider.append_axes("bottom", size="5%", pad=0)

    plt.colorbar(im, cax=cax, label="MJy/sr", orientation="horizontal")

    plt.subplots_adjust(wspace=0.01)

    plt.savefig(f"{plot_name}.png", bbox_inches="tight")
    plt.savefig(f"{plot_name}.pdf", bbox_inches="tight")

    plt.close()

    return True


def parallel_reproject_weight(
    idx,
    files,
    optimal_wcs,
    optimal_shape,
    weight_type="exptime",
):
    """Function to parallelise reprojecting with associated weights

    Args:
        idx: File idx to reproject
        files: Full stack of files
        optimal_wcs: Optimal WCS for image stack
        optimal_shape: Optimal shape for image shape
        weight_type: How to weight the average image. Defaults
            to exptime, the exposure time
    """

    file = files[idx]

    data_array = reproject_image(
        file,
        optimal_wcs=optimal_wcs,
        optimal_shape=optimal_shape,
    )
    # Set any bad data to 0
    data_array.array[np.isnan(data_array.array)] = 0

    if weight_type == "exptime":
        with datamodels.open(file) as model:
            # Create array of exposure time
            exptime = model.meta.exposure.exposure_time
        del model
        weight_array = copy.deepcopy(data_array)
        weight_array.array[np.isfinite(weight_array.array)] = exptime
        weight_array.array[data_array.array == 0] = 0

    elif weight_type == "ivm":
        # Reproject the VAR_RNOISE array and take inverse
        weight_array = reproject_image(
            file,
            optimal_wcs=optimal_wcs,
            optimal_shape=optimal_shape,
            hdu_type="var_rnoise",
        )
        weight_array.array = weight_array.array**-1
        weight_array.array[np.isnan(weight_array.array)] = 0

    else:
        raise ValueError(f"weight_type should be one of {ALLOWED_WEIGHT_TYPES}")

    return idx, (file, data_array, weight_array)


class MultiTileDestripeStep:
    def __init__(
        self,
        in_dir,
        out_dir,
        step_ext,
        procs,
        weight_type="exptime",
        min_area_frac=0.5,
        quadrants=True,
        do_large_scale=False,
        median_filter_factor=4,
        sigma=3,
        dilate_size=7,
        maxiters=None,
        overwrite=False,
    ):
        """Subtracts large-scale stripes using dither information

        Create a weighted mean image of all overlapping files, then do a sigma-clipped
        median along columns and rows (optionally by quadrants), and finally a smoothed clip along
        rows after boxcar filtering to remove persistent large-scale ripples in the data

        Args:
            in_dir: Input directory
            out_dir: Output directory
            step_ext: .fits extension for the files going
                into the step
            procs: Number of processes to run in parallel
            weight_type: How to weight the stacked image.
                Defaults to 'exptime'
            do_large_scale: Whether to do boxcar-filtering to try and remove large,
                consistent ripples between data. Defaults to False
            median_filter_factor: Factor by which we smooth in terms of the array size.
                Defaults to 4, i.e. smoothing scale is 1/4th the array size
            sigma: sigma value for sigma-clipped statistics. Defaults to 3
            dilate_size: Dilation size for mask creation. Defaults to 7
            maxiters: Maximum number of sigma-clipping iterations. Defaults to None
            overwrite: Whether to overwrite or not. Defaults
                to False
        """

        if weight_type not in ALLOWED_WEIGHT_TYPES:
            raise ValueError(
                f"weight_type should be one of {ALLOWED_WEIGHT_TYPES}, not {weight_type}"
            )

        self.in_dir = in_dir
        self.out_dir = out_dir
        self.plot_dir = os.path.join(
            self.out_dir,
            "plots",
        )
        self.step_ext = step_ext
        self.procs = procs

        self.weight_type = weight_type
        self.min_area_frac = min_area_frac
        self.quadrants = quadrants
        self.do_large_scale = do_large_scale
        self.median_filter_factor = median_filter_factor
        self.sigma = sigma
        self.dilate_size = dilate_size
        self.maxiters = maxiters
        self.overwrite = overwrite

        self.files_reproj = None
        self.optimal_wcs = None
        self.optimal_shape = None

    def do_step(self):
        """Run multi-tile destriping"""

        if self.overwrite:
            shutil.rmtree(self.out_dir)

        if not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)
        if not os.path.exists(self.plot_dir):
            os.makedirs(self.plot_dir)

        # Check if we've already run the step
        step_complete_file = os.path.join(
            self.out_dir,
            "multi_tile_destripe_step_complete.txt",
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

        # Create weighted images
        success = self.weighted_reproject_image(
            files,
            procs=procs,
        )
        if not success:
            log.warning("Error in creating reproject stack")
            return False

        success = self.run_multi_tile_destripe(
            procs=procs,
        )

        # If not everything has succeeded, then return a warning
        if not success:
            log.warning("Failures detected in multi-tile destriping")
            return False

        with open(step_complete_file, "w+") as f:
            f.close()

        return True

    def weighted_reproject_image(
        self,
        files,
        procs=1,
    ):
        """Get weighted mean reprojected image

        Args:
            files (list): Files to reproject
            procs (int): Number of processes to use. Defaults to 1.
        """

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            optimal_wcs, optimal_shape = find_optimal_celestial_wcs(files, hdu_in="SCI")

        self.optimal_wcs = optimal_wcs
        self.optimal_shape = optimal_shape

        global data_reproj, weight_reproj
        data_reproj = [None] * len(files)
        weight_reproj = [None] * len(files)
        files_reproj = [None] * len(files)

        log.info("Creating weighted mean images")

        with mp.get_context("fork").Pool(procs) as pool:
            for i, result in tqdm(
                pool.imap_unordered(
                    partial(
                        parallel_reproject_weight,
                        files=files,
                        optimal_wcs=optimal_wcs,
                        optimal_shape=optimal_shape,
                        weight_type=self.weight_type,
                    ),
                    range(len(files)),
                ),
                total=len(files),
                desc="Weighted reprojects",
                ascii=True,
            ):
                files_reproj[i] = result[0]
                data_reproj[i] = result[1]
                weight_reproj[i] = result[2]

            pool.close()
            pool.join()
            gc.collect()

        self.files_reproj = files_reproj

        return True

    def run_multi_tile_destripe(
        self,
        procs=1,
    ):
        """Wrap parallelism around the multi-tile destriping

        Args:
            procs: Number of parallel processes. Defaults to 1
        """

        log.info("Running multi-tile destripe")

        with mp.get_context("fork").Pool(procs) as pool:
            results = [None] * len(self.files_reproj)

            for i, result in tqdm(
                pool.imap_unordered(
                    partial(
                        self.parallel_multi_tile_destripe,
                    ),
                    range(len(self.files_reproj)),
                ),
                total=len(self.files_reproj),
                ascii=True,
                desc="Multi-tile destriping",
            ):
                results[i] = result

            pool.close()
            pool.join()
            gc.collect()

        for result in results:
            if result is not None:
                in_file = result[0]
                stripes = result[1]

                short_file = os.path.split(in_file)[-1]
                out_file = os.path.join(
                    self.out_dir,
                    short_file,
                )

                with datamodels.open(in_file) as im:
                    im.data -= stripes
                    im.save(out_file)
                del im
        gc.collect()

        return True

    def parallel_multi_tile_destripe(self, idx):
        """Function to parallelise up multi-tile destriping

        Args:
            idx: Index of file to be destriped
        """

        file = self.files_reproj[idx]

        data_avg, imin, imax, jmin, jmax = self.create_subset_reproj_image(
            subset_idx=idx,
        )

        data_avg_wcs = self.optimal_wcs[jmin:jmax, imin:imax]

        result = self.multi_tile_destripe(
            file,
            data_avg_array=data_avg,
            data_avg_wcs=data_avg_wcs,
        )
        return idx, result

    def create_subset_reproj_image(
        self,
        subset_idx,
    ):
        """Create a subset average image from a bunch of reprojected ones

        Args:
            subset_idx: index of image to create the subset average image for
        """

        file_data = copy.deepcopy(data_reproj[subset_idx])
        file_weight = copy.deepcopy(weight_reproj[subset_idx])

        data = np.zeros_like(file_data.array)
        weights = np.zeros_like(file_weight.array)

        # First, put the original image in
        data += file_weight.array * file_data.array
        weights += file_weight.array

        # Get the number of pixels, so that we can remove any small overlaps
        # later
        valid_idx = np.where(
            np.logical_and(
                data != 0,
                np.isfinite(data),
            )
        )
        npix_file = len(valid_idx[0])

        file_imin, file_imax = file_data.imin, file_data.imax
        file_jmin, file_jmax = file_data.jmin, file_data.jmax

        for idx in range(len(data_reproj)):
            if subset_idx == idx:
                continue

            if not data_reproj[subset_idx].overlaps(data_reproj[idx]):
                continue

            data_idx = copy.deepcopy(data_reproj[idx])
            weight_idx = copy.deepcopy(weight_reproj[idx])

            data_i = data_idx.array
            weight_i = weight_idx.array

            # Before putting these in, we need to make sure they're on the same level.
            # Do this by subtracting the median of the difference for the first file
            diff = data_idx - data_reproj[subset_idx]
            diff_weight = weight_idx * weight_reproj[subset_idx]

            diff = diff.array[diff_weight.array > 0]
            diff[diff == 0] = np.nan
            diff = diff[np.isfinite(diff)]

            if len(diff) == 0:
                continue

            # Pull out indices we'll need later
            idx_imin, idx_imax = data_idx.imin, data_idx.imax
            idx_jmin, idx_jmax = data_idx.jmin, data_idx.jmax

            _, diff_med, _ = sigma_clipped_stats(diff, maxiters=10)
            data_i -= diff_med

            imin = max(file_imin, idx_imin)
            imax = min(file_imax, idx_imax)
            jmin = max(file_jmin, idx_jmin)
            jmax = min(file_jmax, idx_jmax)

            if imax < imin:
                imax = imin

            if jmax < jmin:
                jmax = jmin

            final_slice = (
                slice(jmin - file_jmin, jmax - file_jmin),
                slice(imin - file_imin, imax - file_imin),
            )
            reproj_slice = (
                slice(jmin - idx_jmin, jmax - idx_jmin),
                slice(imin - idx_imin, imax - idx_imin),
            )

            # Put data and weights into array
            new_data = np.zeros_like(data)
            new_weights = np.zeros_like(weights)
            new_data[final_slice] += weight_i[reproj_slice] * data_i[reproj_slice]
            new_weights[final_slice] += weight_i[reproj_slice]

            # Check we have the required amount of overlap
            valid_i = new_data[valid_idx]
            valid_i = valid_i[np.logical_and(valid_i != 0, np.isfinite(valid_i))]

            area_frac = len(valid_i) / npix_file

            if area_frac < self.min_area_frac:
                continue

            data += new_data
            weights += new_weights

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            data_avg = data / weights
        data_avg[data_avg == 0] = np.nan

        return data_avg, file_imin, file_imax, file_jmin, file_jmax

    def multi_tile_destripe(
        self,
        file,
        data_avg_array,
        data_avg_wcs,
    ):
        """Do a row-by-row, column-by-column data subtraction using other dither information

        Create a weighted mean image of all overlapping files, then do a sigma-clipped
        median along columns and rows (optionally by quadrants), and finally a smoothed clip along
        rows after boxcar filtering to remove persistent large-scale ripples in the data

        Args:
            file (str): File to correct
            data_avg_array (np.ndarray): Pre-calculated average image
            data_avg_wcs: WCS for the average image
        """

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with datamodels.open(file) as model:
                file_name = model.meta.filename

                quadrants = copy.deepcopy(self.quadrants)
                if "sub" in model.meta.subarray.name.lower():
                    quadrants = False

                dq_bit_mask = get_dq_bit_mask(model.dq)

                # Pull out data and DQ mask
                data = copy.deepcopy(model.data)
                data[dq_bit_mask != 0] = np.nan

                wcs = model.meta.wcs.to_fits_sip()
            del model

            # Reproject the average image
            data_avg = reproject_interp(
                (data_avg_array, data_avg_wcs),
                wcs,
                return_footprint=False,
            )

            # Replace NaNd data with column median, so the boxcar
            # doesn't catastrophically fail later
            data_avg_col = np.nanmedian(data_avg, axis=1)
            for col in range(data_avg.shape[0]):
                col_idx = np.where(np.isnan(data_avg[col, :]))
                data_avg[col, col_idx[0]] = data_avg_col[col]

            diff_unsmoothed = data - data_avg
            diff_unsmoothed -= np.nanmedian(diff_unsmoothed)

            stripes_arr = np.zeros_like(diff_unsmoothed)

            mask_pos = make_source_mask(
                diff_unsmoothed,
                nsigma=self.sigma,
                dilate_size=self.dilate_size,
                sigclip_iters=self.maxiters,
            )
            mask_neg = make_source_mask(
                -diff_unsmoothed,
                mask=mask_pos,
                nsigma=self.sigma,
                dilate_size=self.dilate_size,
                sigclip_iters=self.maxiters,
            )
            mask_unsmoothed = mask_pos | mask_neg  # | dq_bit_mask1

            # First, subtract the y
            stripes_y = sigma_clipped_stats(
                diff_unsmoothed - stripes_arr,
                mask=mask_unsmoothed,
                sigma=self.sigma,
                maxiters=self.maxiters,
                axis=0,
            )[1]

            # Centre around 0, replace NaNs
            stripes_y -= np.nanmedian(stripes_y)
            stripes_y[np.isnan(stripes_y)] = 0

            stripes_arr += stripes_y[np.newaxis, :]

            stripes_x_2d = np.zeros_like(stripes_arr)

            if quadrants:
                quadrant_size = stripes_arr.shape[1] // 4

                for quadrant in range(4):
                    idx_slice = slice(
                        quadrant * quadrant_size, (quadrant + 1) * quadrant_size
                    )

                    # Do a pass at the unsmoothed data
                    diff_quadrants = (
                        diff_unsmoothed[:, idx_slice] - stripes_arr[:, idx_slice]
                    )
                    mask_quadrants = mask_unsmoothed[:, idx_slice]
                    stripes_x = sigma_clipped_stats(
                        diff_quadrants,
                        mask=mask_quadrants,
                        sigma=self.sigma,
                        maxiters=self.maxiters,
                        axis=1,
                    )[1]

                    # Centre around 0, replace NaNs
                    stripes_x -= np.nanmedian(stripes_x)
                    stripes_x[np.isnan(stripes_x)] = 0

                    stripes_x_2d[:, idx_slice] += stripes_x[:, np.newaxis]

            else:
                # Do a pass at the unsmoothed data
                stripes_x = sigma_clipped_stats(
                    diff_unsmoothed - stripes_arr,
                    mask=mask_unsmoothed,
                    sigma=self.sigma,
                    maxiters=self.maxiters,
                    axis=1,
                )[1]

                # Centre around 0, replace NaNs
                stripes_x -= np.nanmedian(stripes_x)
                stripes_x[np.isnan(stripes_x)] = 0

                stripes_x_2d += stripes_x[:, np.newaxis]

            # Centre around 0 one last time
            stripes_x_2d -= np.nanmedian(stripes_x_2d)
            stripes_x_2d[np.isnan(stripes_x_2d)] = 0

            stripes_arr += stripes_x_2d
            stripes_arr -= np.nanmedian(stripes_arr)
            stripes_arr[np.isnan(stripes_arr)] = 0

            if self.do_large_scale:
                # Filter along the y-axis (to preserve the stripe noise) with a large boxcar filter.
                # This ideally flattens out the background, for any consistent large-scale ripples
                # between images
                boxcar = uniform_filter1d(
                    data_avg,
                    size=data_avg.shape[0] // self.median_filter_factor,
                    axis=0,
                    mode="reflect",
                )
                diff_smoothed = data - stripes_arr - boxcar
                diff_smoothed -= np.nanmedian(diff_smoothed)

                mask_pos = make_source_mask(
                    diff_smoothed,
                    nsigma=self.sigma,
                    dilate_size=self.dilate_size,
                    sigclip_iters=self.maxiters,
                )
                mask_neg = make_source_mask(
                    -diff_smoothed,
                    mask=mask_pos,
                    nsigma=self.sigma,
                    dilate_size=self.dilate_size,
                    sigclip_iters=self.maxiters,
                )
                mask_smoothed = mask_pos | mask_neg  # | dq_bit_mask1

                # Pass through the smoothed data, but across the whole image to avoid the bright noisy bits
                stripes_x = sigma_clipped_stats(
                    diff_smoothed,
                    mask=mask_smoothed,
                    sigma=self.sigma,
                    maxiters=self.maxiters,
                    axis=1,
                )[1]

                # Centre around 0, replace NaNs
                stripes_x -= np.nanmedian(stripes_x)
                stripes_x[np.isnan(stripes_x)] = 0

                stripes_arr += stripes_x[:, np.newaxis]

                # Centre around 0 for luck
                stripes_arr -= np.nanmedian(stripes_arr)
                stripes_arr[np.isnan(stripes_arr)] = 0

            # Make diagnostic plot
            plot_name = os.path.join(
                self.plot_dir,
                file_name.replace(".fits", "_dither_stripe_sub"),
            )

            make_diagnostic_plot(
                plot_name=plot_name,
                data=data,
                stripes=stripes_arr,
            )

        return file, stripes_arr
