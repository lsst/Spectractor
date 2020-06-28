import os
import numpy as np
import matplotlib.pyplot as plt

from spectractor import parameters
from spectractor.config import set_logger, load_config
from spectractor.extractor.images import Image, find_target, turn_image
from spectractor.extractor.spectrum import (Spectrum, calibrate_spectrum,
                                            calibrate_spectrum_with_lines)
from spectractor.extractor.background import extract_spectrogram_background_sextractor
from spectractor.extractor.chromaticpsf import ChromaticPSF
from spectractor.extractor.psf import load_PSF
from spectractor.tools import ensure_dir, plot_image_simple, from_lambda_to_colormap, plot_spectrum_simple


def Spectractor(file_name, output_directory, target_label, guess=None, disperser_label="", config='./config/ctio.ini',
                atmospheric_lines=True, line_detection=True):
    """ Spectractor
    Main function to extract a spectrum from an image

    Parameters
    ----------
    file_name: str
        Input file nam of the image to analyse.
    output_directory: str
        Output directory.
    target_label: str
        The name of the targeted object.
    guess: [int,int], optional
        [x0,y0] list of the guessed pixel positions of the target in the image (must be integers). Mandatory if
        WCS solution is absent (default: None).
    disperser_label: str, optional
        The name of the disperser (default: "").
    config: str
        The config file name (default: "./config/ctio.ini").
    atmospheric_lines: bool, optional
        If True atmospheric lines are used in the calibration fit.
    line_detection: bool, optional
        If True the absorption or emission lines are
        used to calibrate the pixel to wavelength relationship.

    Returns
    -------
    spectrum: Spectrum
        The extracted spectrum object.

    Examples
    --------

    Extract the spectrogram and its characteristics from the image:

    .. doctest::

        >>> import os
        >>> from spectractor.logbook import LogBook
        >>> logbook = LogBook(logbook='./ctiofulllogbook_jun2017_v5.csv')
        >>> file_names = ['./tests/data/reduc_20170530_134.fits']
        >>> for file_name in file_names:
        ...     tag = file_name.split('/')[-1]
        ...     disperser_label, target_label, xpos, ypos = logbook.search_for_image(tag)
        ...     if target_label is None or xpos is None or ypos is None:
        ...         continue
        ...     spectrum = Spectractor(file_name, './tests/data/', guess=[xpos, ypos], target_label=target_label,
        ...                            disperser_label=disperser_label, config='./config/ctio.ini')

    .. doctest::
        :hide:

        >>> assert spectrum is not None
        >>> assert os.path.isfile('tests/data/educ_20170530_134_spectrum.fits')

    """

    my_logger = set_logger(__name__)
    my_logger.info('\n\tStart SPECTRACTOR')
    # Load config file
    load_config(config)

    # Load reduced image
    image = Image(file_name, target_label=target_label, disperser_label=disperser_label)
    if parameters.DEBUG:
        image.plot_image(scale='symlog', target_pixcoords=guess)
    # Set output path
    ensure_dir(output_directory)
    output_filename = file_name.split('/')[-1]
    output_filename = output_filename.replace('.fits', '_spectrum.fits')
    output_filename = output_filename.replace('.fz', '_spectrum.fits')
    output_filename = os.path.join(output_directory, output_filename)
    output_filename_spectrogram = output_filename.replace('spectrum', 'spectrogram')
    output_filename_psf = output_filename.replace('spectrum.fits', 'table.csv')
    # Find the exact target position in the raw cut image: several methods
    my_logger.info('\n\tSearch for the target in the image...')
    find_target(image, guess, use_wcs=True)
    # Rotate the image
    turn_image(image)
    # Find the exact target position in the rotated image: several methods
    my_logger.info('\n\tSearch for the target in the rotated image...')
    find_target(image, guess, rotated=True, use_wcs=True)
    # Create Spectrum object
    spectrum = Spectrum(image=image)
    # Subtract background and bad pixels
    extract_spectrum_from_image(image, spectrum, signal_width=parameters.PIXWIDTH_SIGNAL,
                                ws=(parameters.PIXDIST_BACKGROUND,
                                    parameters.PIXDIST_BACKGROUND + parameters.PIXWIDTH_BACKGROUND),
                                right_edge=parameters.CCD_IMSIZE - 200)
    spectrum.atmospheric_lines = atmospheric_lines
    # Calibrate the spectrum
    calibrate_spectrum(spectrum)
    if line_detection:
        my_logger.info('\n\tCalibrating order %d spectrum...' % spectrum.order)
        calibrate_spectrum_with_lines(spectrum)
    else:
        spectrum.header['WARNINGS'] = 'No calibration procedure with spectral features.'
    # Save the spectrum
    spectrum.save_spectrum(output_filename, overwrite=True)
    spectrum.save_spectrogram(output_filename_spectrogram, overwrite=True)
    spectrum.lines.print_detected_lines(output_file_name=output_filename.replace('_spectrum.fits', '_lines.csv'),
                                        overwrite=True, amplitude_units=spectrum.units)
    # Plot the spectrum
    if parameters.VERBOSE and parameters.DISPLAY:
        spectrum.plot_spectrum(xlim=None)
    distance = spectrum.chromatic_psf.get_distance_along_dispersion_axis()
    lambdas = np.interp(distance, spectrum.pixels, spectrum.lambdas)
    spectrum.chromatic_psf.table['lambdas'] = lambdas
    spectrum.chromatic_psf.table.write(output_filename_psf, overwrite=True)
    return spectrum


def extract_spectrum_from_image(image, spectrum, signal_width=10, ws=(20, 30), right_edge=parameters.CCD_IMSIZE - 200):
    """Extract the 1D spectrum from the image.

    Method : remove a uniform background estimated from the rectangular lateral bands

    The spectrum amplitude is the sum of the pixels in the 2*w rectangular window
    centered on the order 0 y position.
    The up and down backgrounds are estimated as the median in rectangular regions
    above and below the spectrum, in the ws-defined rectangular regions; stars are filtered
    as nan values using an hessian analysis of the image to remove structures.
    The subtracted background is the mean of the two up and down backgrounds.
    Stars are filtered.

    Prerequisites: the target position must have been found before, and the
        image turned to have an horizontal dispersion line

    Parameters
    ----------
    image: Image
        Image object from which to extract the spectrum
    spectrum: Spectrum
        Spectrum object to store new wavelengths, data and error arrays
    signal_width: int
        Half width of central region where the spectrum is extracted and summed (default: 10)
    ws: list
        up/down region extension where the sky background is estimated with format [int, int] (default: [20,30])
    right_edge: int
        Right-hand pixel position above which no pixel should be used (default: 1800)
    """

    if parameters.PSF_EXTRACTION_MODE != "PSF_1D" and parameters.PSF_EXTRACTION_MODE != "PSF_2D":
        raise NotImplementedError(f"PSF_EXTRACTION_MODE must PSF_1D or PSF_2D. Found {parameters.PSF_EXTRACTION_MODE}.")
    my_logger = set_logger(__name__)
    if ws is None:
        ws = [signal_width+20, signal_width+30]
    my_logger.info(
        f'\n\tExtracting spectrum from image: spectrum with width 2*{signal_width:d} pixels '
        f'and background from {ws[0]:d} to {ws[1]:d} pixels')

    # Make a data copy
    data = np.copy(image.data_rotated)[:, 0:right_edge]
    err = np.copy(image.stat_errors_rotated)[:, 0:right_edge]

    # Lateral bands to remove sky background
    Ny, Nx = data.shape
    y0 = int(image.target_pixcoords_rotated[1])
    ymax = min(Ny, y0 + ws[1])
    ymin = max(0, y0 - ws[1])

    # Roughly estimates the wavelengths and set start 0 nm before parameters.LAMBDA_MIN
    # and end 0 nm after parameters.LAMBDA_MAX
    lambdas = image.disperser.grating_pixel_to_lambda(np.arange(Nx) - image.target_pixcoords_rotated[0],
                                                      x0=image.target_pixcoords)
    xmin = int(np.argmin(np.abs(lambdas - (parameters.LAMBDA_MIN - 0))))
    xmax = min(right_edge, int(np.argmin(np.abs(lambdas - (parameters.LAMBDA_MAX + 0)))))

    # Create spectrogram
    data = data[ymin:ymax, xmin:xmax]
    err = err[ymin:ymax, xmin:xmax]
    Ny, Nx = data.shape
    my_logger.info(
        f'\n\tExtract spectrogram: crop rotated image [{xmin}:{xmax},{ymin}:{ymax}] (size ({Nx}, {Ny}))')

    # Position of the order 0 in the spectrogram coordinates
    target_pixcoords_spectrogram = [image.target_pixcoords_rotated[0] - xmin, image.target_pixcoords_rotated[1] - ymin]

    # Extract the background on the rotated image
    bgd_index = np.concatenate((np.arange(0, Ny//2 - ws[0]), np.arange(Ny//2 + ws[0], Ny))).astype(int)
    bgd_model_func = extract_spectrogram_background_sextractor(data, err, ws=ws)
    bgd_res = ((data - bgd_model_func(np.arange(Nx), np.arange(Ny)))/err)[bgd_index]
    # while np.nanmean(bgd_res)/np.nanstd(bgd_res) < -0.2 and parameters.PIXWIDTH_BOXSIZE >= 5:
    my_logger.warning(f"{np.abs(np.nanmean(bgd_res))} {np.nanstd(bgd_res)}")
    while (np.abs(np.nanmean(bgd_res)) > 1 or np.nanstd(bgd_res) > 2) and parameters.PIXWIDTH_BOXSIZE >= 5:
        parameters.PIXWIDTH_BOXSIZE = max(5, parameters.PIXWIDTH_BOXSIZE // 2)
        my_logger.warning(f"\n\tPull distribution of background residuals has a negative mean which may lead to "
                          f"background over-subtraction: mean(pull)/RMS(pull)={np.nanmean(bgd_res)/np.nanstd(bgd_res)}."
                          f"This value should be greater than -0.5. To do so, parameters.PIXWIDTH_BOXSIZE is divided "
                          f"by 2 from {parameters.PIXWIDTH_BOXSIZE*2} -> {parameters.PIXWIDTH_BOXSIZE}.")
        bgd_model_func = extract_spectrogram_background_sextractor(data, err, ws=ws)
        bgd_res = ((data - bgd_model_func(np.arange(Nx), np.arange(Ny)))/err)[bgd_index]

    # Propagate background uncertainties
    # err = np.sqrt(err*err+bgd_model_func(np.arange(Nx),np.arange(Ny))/image.gain[ymin:ymax,pixel_start:pixel_end]**2)

    # Fit the transverse profile
    my_logger.info(f'\n\tStart PSF1D transverse fit...')
    psf = load_PSF(psf_type=parameters.PSF_TYPE)
    s = ChromaticPSF(psf, Nx=Nx, Ny=Ny, x0=target_pixcoords_spectrogram[0], y0=target_pixcoords_spectrogram[1],
                     deg=parameters.PSF_POLY_ORDER, saturation=image.saturation)
    s.fit_transverse_PSF1D_profile(data, err, signal_width, ws, pixel_step=10, sigma_clip=5,
                                   bgd_model_func=bgd_model_func, saturation=image.saturation, live_fit=False)

    # Fill spectrum object
    spectrum.pixels = np.arange(xmin, xmax, 1).astype(int)
    spectrum.data = np.copy(s.table['amplitude'])
    spectrum.err = np.copy(s.table['flux_err'])
    my_logger.debug(f'\n\tTransverse fit table:\n{s.table}')
    if parameters.DEBUG:
        s.plot_summary()

    # Fit the data:
    method = "noprior"
    mode = "1D"
    my_logger.info(f'\n\tStart ChromaticPSF polynomial fit with '
                   f'mode={mode} and amplitude_priors_method={method}...')
    w = s.fit_chromatic_psf(data, bgd_model_func=bgd_model_func, data_errors=err,
                            amplitude_priors_method=method, mode=mode, verbose=parameters.VERBOSE)
    spectrum.data = np.copy(w.amplitude_params)
    spectrum.err = np.copy(w.amplitude_params_err)
    spectrum.cov_matrix = np.copy(w.amplitude_cov_matrix)
    spectrum.chromatic_psf = s

    Dx_rot = spectrum.pixels.astype(float) - image.target_pixcoords_rotated[0]
    s.table['Dx'] = np.copy(Dx_rot)
    s.table['Dy'] = s.table['y_c'] - (image.target_pixcoords_rotated[1] - ymin)
    s.table['Dy_disp_axis'] = 0
    s.table['Dy_fwhm_inf'] = s.table['Dy'] - 0.5 * s.table['fwhm']
    s.table['Dy_fwhm_sup'] = s.table['Dy'] + 0.5 * s.table['fwhm']
    my_logger.debug(f"\n\tTransverse fit table before derotation:"
                    f"\n{s.table[['amplitude', 'x_c', 'y_c', 'Dx', 'Dy', 'Dy_disp_axis']]}")

    # Rotate and save the table
    s.rotate_table(-image.rotation_angle)
    flux = np.copy(s.table["amplitude"])
    flux_err = np.copy(s.table["flux_err"])

    # Extract the spectrogram edges
    data = np.copy(image.data)[:, 0:right_edge]
    err = np.copy(image.stat_errors)[:, 0:right_edge]
    Ny, Nx = data.shape
    x0 = int(image.target_pixcoords[0])
    y0 = int(image.target_pixcoords[1])
    ymax = min(Ny, y0 + int(s.table['Dy_disp_axis'].max()) + ws[1] + 1)  # +1 to  include edges
    ymin = max(0, y0 + int(s.table['Dy_disp_axis'].min()) - ws[1])
    distance = s.get_distance_along_dispersion_axis()
    lambdas = image.disperser.grating_pixel_to_lambda(distance, x0=image.target_pixcoords)
    lambda_min_index = int(np.argmin(np.abs(lambdas - (parameters.LAMBDA_MIN - 0))))
    lambda_max_index = int(np.argmin(np.abs(lambdas - (parameters.LAMBDA_MAX + 0))))
    xmin = int(s.table['Dx'][lambda_min_index] + x0)
    xmax = min(right_edge, int(s.table['Dx'][lambda_max_index] + x0) + 1)  # +1 to  include edges

    # Position of the order 0 in the spectrogram coordinates
    target_pixcoords_spectrogram = [image.target_pixcoords[0] - xmin, image.target_pixcoords[1] - ymin]
    s.y0 = target_pixcoords_spectrogram[1]
    s.x0 = target_pixcoords_spectrogram[0]

    # Update y_c and x_c after rotation
    s.table['y_c'] = s.table['Dy'] + target_pixcoords_spectrogram[1]
    s.table['x_c'] = s.table['Dx'] + target_pixcoords_spectrogram[0]
    my_logger.debug(f"\n\tTransverse fit table after derotation:"
                    f"\n{s.table[['amplitude', 'x_c', 'y_c', 'Dx', 'Dy', 'Dy_disp_axis']]}")

    # Create spectrogram
    data = data[ymin:ymax, xmin:xmax]
    err = err[ymin:ymax, xmin:xmax]
    Ny, Nx = data.shape

    # Extract the non rotated background
    bgd_model_func = extract_spectrogram_background_sextractor(data, err, ws=ws)
    bgd = bgd_model_func(np.arange(Nx), np.arange(Ny))

    # Propagate background uncertainties
    # err = np.sqrt(err*err + bgd_model_func(np.arange(Nx), np.arange(Ny))/image.gain[ymin:ymax, xmin:xmax]**2)

    # 2D extraction
    opt_reg = -1
    if parameters.PSF_EXTRACTION_MODE == "PSF_2D":
        # build 1D priors
        psf_poly_priors = s.from_table_to_poly_params()[s.Nx:]
        Dy_disp_axis = np.copy(s.table["Dy_disp_axis"])
        # initialize a new ChromaticPSF
        s = ChromaticPSF(psf, Nx=Nx, Ny=Ny, x0=target_pixcoords_spectrogram[0], y0=target_pixcoords_spectrogram[1],
                         deg=parameters.PSF_POLY_ORDER, saturation=image.saturation)
        # fill a first table with first guess
        s.table['Dx'] = np.arange(xmin, xmax, 1) - image.target_pixcoords[0]
        s.table["amplitude"] = np.interp(s.table['Dx'], Dx_rot, flux)
        s.table["flux_err"] = np.interp(s.table['Dx'], Dx_rot, flux_err)
        s.table['Dy_disp_axis'] = np.interp(s.table['Dx'], Dx_rot, Dy_disp_axis)
        s.poly_params = np.concatenate((s.table["amplitude"], psf_poly_priors))
        s.profile_params = s.from_poly_params_to_profile_params(s.poly_params, apply_bounds=True)
        s.fill_table_with_profile_params(s.profile_params)
        s.table['Dy'] = s.table['y_c'] - target_pixcoords_spectrogram[1]
        # deconvolve and regularize with 1D priors
        method = "psf1d"
        mode = "2D"
        my_logger.info(f'\n\tStart ChromaticPSF polynomial fit with '
                       f'mode={mode} and amplitude_priors_method={method}...')
        my_logger.debug(f"\n\tTransverse fit table before PSF_2D fit:"
                        f"\n{s.table[['amplitude', 'x_c', 'y_c', 'Dx', 'Dy', 'Dy_disp_axis']]}")
        # w = s.fit_chromatic_psf(data, bgd_model_func=bgd_model_func, data_errors=err,
        #                         amplitude_priors_method="fixed", mode=mode, verbose=parameters.VERBOSE)
        w = s.fit_chromatic_psf(data, bgd_model_func=bgd_model_func, data_errors=err,
                                amplitude_priors_method=method, mode=mode, verbose=parameters.VERBOSE)
        # save results
        spectrum.spectrogram_fit = s.evaluate(s.poly_params, mode=mode)
        spectrum.spectrogram_residuals = (data - spectrum.spectrogram_fit - bgd_model_func(np.arange(Nx),
                                                                                           np.arange(Ny))) / err
        spectrum.data = np.copy(w.amplitude_params)
        spectrum.err = np.copy(w.amplitude_params_err)
        spectrum.cov_matrix = np.copy(w.amplitude_cov_matrix)
        spectrum.pixels = np.copy(s.table['Dx'])
        s.table['Dy'] = s.table['y_c'] - target_pixcoords_spectrogram[1]
        s.table['Dy_fwhm_inf'] = s.table['Dy'] - 0.5 * s.table['fwhm']
        s.table['Dy_fwhm_sup'] = s.table['Dy'] + 0.5 * s.table['fwhm']
        spectrum.chromatic_psf = s
        opt_reg = s.opt_reg
    spectrum.header['PSF_REG'] = opt_reg

    # First guess for lambdas
    first_guess_lambdas = image.disperser.grating_pixel_to_lambda(s.get_distance_along_dispersion_axis(),
                                                                  x0=image.target_pixcoords)
    s.table['lambdas'] = first_guess_lambdas
    spectrum.lambdas = np.array(first_guess_lambdas)

    # Position of the order 0 in the spectrogram coordinates
    my_logger.info(f'\n\tExtract spectrogram: crop image [{xmin}:{xmax},{ymin}:{ymax}] (size ({Nx}, {Ny}))'
                   f'\n\tNew target position in spectrogram frame: {target_pixcoords_spectrogram}')

    # Save results
    spectrum.spectrogram = data
    spectrum.spectrogram_err = err
    spectrum.spectrogram_bgd = bgd
    spectrum.spectrogram_x0 = target_pixcoords_spectrogram[0]
    spectrum.spectrogram_y0 = target_pixcoords_spectrogram[1]
    spectrum.spectrogram_xmin = xmin
    spectrum.spectrogram_xmax = xmax
    spectrum.spectrogram_ymin = ymin
    spectrum.spectrogram_ymax = ymax
    spectrum.spectrogram_deg = spectrum.chromatic_psf.deg
    spectrum.spectrogram_saturation = spectrum.chromatic_psf.saturation

    # Plot FHWM(lambda)
    if parameters.DEBUG:
        fig, ax = plt.subplots(2, 1, figsize=(10, 8), sharex="all")
        ax[0].plot(spectrum.lambdas, np.array(s.table['fwhm']))
        ax[0].set_xlabel(r"$\lambda$ [nm]")
        ax[0].set_ylabel("Transverse FWHM [pixels]")
        ax[0].set_ylim((0.8 * np.min(s.table['fwhm']), 1.2 * np.max(s.table['fwhm'])))  # [-10:])))
        ax[0].grid()
        ax[1].plot(spectrum.lambdas, np.array(s.table['y_c']))
        ax[1].set_xlabel(r"$\lambda$ [nm]")
        ax[1].set_ylabel("Distance from mean dispersion axis [pixels]")
        # ax[1].set_ylim((0.8*np.min(s.table['Dy']), 1.2*np.max(s.table['fwhm'][-10:])))
        ax[1].grid()
        if parameters.DISPLAY:
            plt.show()
        if parameters.LSST_SAVEFIGPATH:
            fig.savefig(os.path.join(parameters.LSST_SAVEFIGPATH, 'fwhm.pdf'))

    # Summary plot
    if parameters.DEBUG or parameters.LSST_SAVEFIGPATH:
        fig, ax = plt.subplots(3, 1, sharex='all', figsize=(12, 6))
        xx = np.arange(s.table['Dx'].size)
        plot_image_simple(ax[2], data=data, scale="symlog", title='', units=image.units, aspect='auto')
        ax[2].plot(xx, target_pixcoords_spectrogram[1] + s.table['Dy_disp_axis'], label='Dispersion axis')
        ax[2].plot(xx, target_pixcoords_spectrogram[1] + s.table['Dy_disp_axis'], label='Dispersion axis')
        ax[2].scatter(xx, target_pixcoords_spectrogram[1] + s.table['Dy'],
                      c=s.table['lambdas'], edgecolors='None', cmap=from_lambda_to_colormap(s.table['lambdas']),
                      label='Fitted spectrum centers', marker='o', s=10)
        ax[2].plot(xx, target_pixcoords_spectrogram[1] + s.table['Dy_fwhm_inf'], 'k-', label='Fitted FWHM')
        ax[2].plot(xx, target_pixcoords_spectrogram[1] + s.table['Dy_fwhm_sup'], 'k-', label='')
        ax[2].set_ylim(0.5 * Ny - signal_width, 0.5 * Ny + signal_width)
        ax[2].set_xlim(0, xx.size)
        ax[2].legend(loc='best')
        plot_spectrum_simple(ax[0], np.arange(spectrum.data.size), spectrum.data, data_err=spectrum.err,
                             units=image.units, label='Fitted spectrum', xlim=[0, spectrum.data.size])
        ax[0].plot(xx, s.table['flux_sum'], 'k-', label='Cross spectrum')
        ax[0].set_xlim(0, xx.size)
        ax[0].legend(loc='best')
        ax[1].plot(xx, (s.table['flux_sum'] - s.table['flux_integral']) / s.table['flux_sum'],
                   label='(model_integral-cross_sum)/cross_sum')
        ax[1].legend()
        ax[1].grid(True)
        ax[1].set_ylim(-1, 1)
        ax[1].set_ylabel('Relative difference')
        fig.tight_layout()
        fig.subplots_adjust(hspace=0)
        pos0 = ax[0].get_position()
        pos1 = ax[1].get_position()
        pos2 = ax[2].get_position()
        ax[0].set_position([pos2.x0, pos0.y0, pos2.width, pos0.height])
        ax[1].set_position([pos2.x0, pos1.y0, pos2.width, pos1.height])
        if parameters.DISPLAY:
            plt.show()
        if parameters.LSST_SAVEFIGPATH:
            fig.savefig(os.path.join(parameters.LSST_SAVEFIGPATH, 'spectrum.pdf'))
    return spectrum
