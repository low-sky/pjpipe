import gc
import glob
import json
import logging
import os
import shutil
import time

import jwst
import numpy as np
from jwst.datamodels import ModelContainer
from jwst.pipeline import calwebb_image3
from jwst.skymatch import SkyMatchStep
from jwst.tweakreg import TweakRegStep

from ..utils import (
    get_band_type,
    get_band_ext,
    get_obs_table,
    parse_parameter_dict,
    attribute_setter,
    recursive_setattr,
    fwhms_pix,
)

log = logging.getLogger("stpipe")
log.addHandler(logging.NullHandler())


class Lv3Step:
    def __init__(
        self,
        target,
        band,
        in_dir,
        out_dir,
        is_bgr,
        step_ext,
        procs,
        tweakreg_group_dithers=None,
        skymatch_group_dithers=None,
        skymatch_degroup_dithers=None,
        bgr_check_type="parallel_off",
        bgr_background_name="off",
        process_bgr_like_science=False,
        jwst_parameters=None,
        overwrite=False,
    ):
        """Wrapper around the level 3 JWST pipeline

        Args:
            target: Target to consider
            band: Band to consider
            in_dir: Input directory
            out_dir: Output directory
            is_bgr: Whether we're processing background observations or not
            step_ext: .fits extension for the files going
                into the lv3 pipeline
            procs: Number of processes to run in parallel
            tweakreg_group_dithers: List of 'miri',
                'nircam_long', 'nircam_short' of whether to group
                up dithers for tweakreg. Defaults to None, which will
                keep at default
            skymatch_group_dithers: List of 'miri', 'nircam_long',
                'nircam_short' of whether to group up dithers for
                skymatch. Defaults to None, which will keep at
                default
            skymatch_degroup_dithers: List of 'miri', 'nircam_long',
                'nircam_short' of whether to group up dithers for
                skymatch. Defaults to None, which will keep at
                default.
            bgr_check_type: Method to check if obs is science
                or background. Options are 'parallel_off' and
                'check_in_name'. Defaults to 'parallel_off'
            bgr_background_name: If `bgr_check_type` is 'check_in_name',
                this is the string to match
            process_bgr_like_science: If True, will process background
                images as if they are science images. Defaults to False
            jwst_parameters: Parameter dictionary to pass to
                the level 2 pipeline. Defaults to None,
                which will run the observatory defaults
            overwrite: Whether to overwrite or not. Defaults
                to False
        """
        if jwst_parameters is None:
            jwst_parameters = {}

        self.target = target
        self.band = band
        self.in_dir = in_dir
        self.out_dir = out_dir
        self.is_bgr = is_bgr
        self.step_ext = step_ext
        self.procs = procs
        self.tweakreg_group_dithers = tweakreg_group_dithers
        self.skymatch_group_dithers = skymatch_group_dithers
        self.skymatch_degroup_dithers = skymatch_degroup_dithers
        self.bgr_check_type = bgr_check_type
        self.bgr_background_name = bgr_background_name
        self.process_bgr_like_science = process_bgr_like_science
        self.jwst_parameters = jwst_parameters
        self.overwrite = overwrite

        self.band_type = get_band_type(self.band)
        self.band_ext = get_band_ext(self.band)

    def do_step(self):
        """Run the level 3 pipeline"""

        if self.overwrite:
            shutil.rmtree(self.out_dir)
            os.system(f"rm -rf {os.path.join(self.in_dir, '*.json')}")

        if not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)

        # Check if we've already run the step
        step_complete_file = os.path.join(
            self.out_dir,
            "lv3_step_complete.txt",
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

        asn_file = self.create_asn_file(
            files=files,
        )

        success = self.run_step(
            asn_file,
        )

        # If not everything has succeeded, then return a warning
        if not success:
            log.warning("Failures detected in level 3 pipeline")
            return False

        with open(step_complete_file, "w+") as f:
            f.close()

        return True

    def create_asn_file(
        self,
        files,
    ):
        """Setup asn lv3 file"""

        log.info("Building asn file")

        check_bgr = True

        # If we have NIRCam operating with parallel offs, switch off background checking
        # else everything will be flagged as backgrounds
        if self.band_type == "nircam" and self.bgr_check_type == "parallel_off":
            check_bgr = False

        ending = ""
        if self.process_bgr_like_science:
            ending += "_offset"
        asn_lv3_filename = os.path.join(
            self.in_dir,
            f"asn_lv3_{self.band}{ending}.json",
        )

        # Filter out background obs
        if not self.process_bgr_like_science:
            filtered_files = [f for f in files if "offset" not in f]
        else:
            filtered_files = [f for f in files if "offset" in f]

        filtered_files.sort()

        tab = get_obs_table(
            files=filtered_files,
            check_bgr=check_bgr,
            check_type=self.bgr_check_type,
            background_name=self.bgr_background_name,
        )

        if self.is_bgr:
            bgr_ext = "_bgr"
        else:
            bgr_ext = ""

        json_content = {
            "asn_type": "None",
            "asn_rule": "DMS_Level3_Base",
            "version_id": time.strftime("%Y%m%dt%H%M%S"),
            "code_version": jwst.__version__,
            "degraded_status": "No known degraded exposures in association.",
            "program": tab["Program"][0],
            "constraints": "No constraints",
            "asn_id": f"o{tab['Obs_ID'][0]}",
            "asn_pool": "none",
            "products": [
                {
                    "name": f"{self.target.lower()}_{self.band_type}_lv3_{self.band.lower()}{bgr_ext}",
                    "members": [],
                }
            ],
        }

        # If we're only processing background, flip the switch
        if self.is_bgr:
            tab["Type"] = "sci"

        # If we're not including backgrounds, filter them out here
        if not self.process_bgr_like_science:
            tab = tab[tab["Type"] == "sci"]

        for row in tab:
            json_content["products"][-1]["members"].append(
                {"expname": row["File"], "exptype": "science", "exposerr": "null"}
            )

        with open(asn_lv3_filename, "w") as f:
            json.dump(json_content, f)

        return asn_lv3_filename

    def run_step(
        self,
        asn_file,
    ):
        """Run the level 3 step

        Args:
            asn_file: Path to association JSON file
        """

        log.info("Running level 3 pipeline")

        _, short_long = get_band_type(self.band, short_long_nircam=True)

        # FWHM should be set per-band for both tweakreg and source catalogue
        fwhm_pix = fwhms_pix[self.band]

        # Set up to run lv3 pipeline

        config = calwebb_image3.Image3Pipeline.get_config_from_reference(asn_file)
        im3 = calwebb_image3.Image3Pipeline.from_config_section(config)
        im3.output_dir = self.out_dir

        im3.tweakreg.kernel_fwhm = fwhm_pix * 2
        im3.source_catalog.kernel_fwhm = fwhm_pix * 2

        im3 = attribute_setter(
            im3,
            parameters=self.jwst_parameters,
            band=self.band,
            target=self.target,
        )

        # Load the asn file in, so we have access to everything we need later
        asn_file = ModelContainer(asn_file)

        # Run the tweakreg step
        config = TweakRegStep.get_config_from_reference(asn_file)
        tweakreg = TweakRegStep.from_config_section(config)
        tweakreg.output_dir = self.out_dir
        tweakreg.save_results = False
        tweakreg.kernel_fwhm = fwhm_pix * 2

        try:
            tweakreg_params = self.jwst_parameters["tweakreg"]
        except KeyError:
            tweakreg_params = {}

        for tweakreg_key in tweakreg_params:
            value = parse_parameter_dict(
                parameters=tweakreg_params,
                key=tweakreg_key,
                band=self.band,
                target=self.target,
            )

            if value == "VAL_NOT_FOUND":
                continue

            recursive_setattr(tweakreg, tweakreg_key, value)

        # Group up the dithers
        if short_long in self.tweakreg_group_dithers:
            for model in asn_file._models:
                model.meta.observation.exposure_number = "1"

        asn_file = tweakreg.run(asn_file)

        del tweakreg
        gc.collect()

        # Make sure we skip tweakreg since we've already done it
        im3.tweakreg.skip = True

        # Degroup again to avoid potential weirdness later
        if short_long in self.tweakreg_group_dithers:
            for i, model in enumerate(asn_file._models):
                model.meta.observation.exposure_number = str(i)

        # Run the skymatch step with custom hacks if required
        config = SkyMatchStep.get_config_from_reference(asn_file)
        skymatch = SkyMatchStep.from_config_section(config)
        skymatch.output_dir = self.out_dir
        skymatch.save_results = False

        try:
            skymatch_params = self.jwst_parameters["skymatch"]
        except KeyError:
            skymatch_params = {}

        for skymatch_key in skymatch_params:
            value = parse_parameter_dict(
                parameters=skymatch_params,
                key=skymatch_key,
                band=self.band,
                target=self.target,
            )

            if value == "VAL_NOT_FOUND":
                continue

            recursive_setattr(skymatch, skymatch_key, value)

        # Group or degroup for skymatching
        if short_long in self.skymatch_group_dithers:
            for model in asn_file._models:
                model.meta.observation.exposure_number = "1"

        elif short_long in self.skymatch_degroup_dithers:
            for i, model in enumerate(asn_file._models):
                model.meta.observation.exposure_number = str(i)

        asn_file = skymatch.run(asn_file)

        del skymatch
        gc.collect()

        # Degroup again to avoid potential weirdness later
        if short_long in self.skymatch_group_dithers:
            for i, model in enumerate(asn_file._models):
                model.meta.observation.exposure_number = str(i)

        im3.skymatch.skip = True

        # Run the rest of the level 3 pipeline
        im3.run(asn_file)

        del im3
        del asn_file
        gc.collect()

        return True
