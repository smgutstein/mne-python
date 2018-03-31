# Authors: Mainak Jas <mainak.jas@telecom-paristech.fr>
#          Jona Sassenhagen <jona.sassenhagen@gmail.com>
#
# License: BSD (3-clause)

import os.path as op

import numpy as np
import sys
from collections import Mapping
from ..utils import (_read_segments_file, _find_channels,
                     _synthesize_stim_channel)
from ..constants import FIFF, Bunch
from ..meas_info import _empty_info, create_info
from ..base import BaseRaw, _check_update_montage
from ...utils import logger, verbose, check_version, warn
from ...channels.montage import Montage
from ...epochs import BaseEpochs
from ...event import read_events
from ...externals.six import string_types
from ...annotations import Annotations

# just fix the scaling for now, EEGLAB doesn't seem to provide this info
CAL = 1e-6


def _check_fname(fname):
    """Check if the file extension is valid."""
    fmt = str(op.splitext(fname)[-1])
    if fmt == '.dat':
        raise NotImplementedError(
            'Old data format .dat detected. Please update your EEGLAB '
            'version and resave the data in .fdt format')
    elif fmt != '.fdt':
        raise IOError('Expected .fdt file format. Found %s format' % fmt)


def _check_for_ascii_filename(eeg, input_fname):

    """Checks to see if eeg.data is array of ascii values
       of filename. Does not check if extension is valid
       (or if it exists), since this is handled by _check_fname"""

    if (isinstance(eeg.data, np.ndarray) and
       len(eeg.data.shape) == 1 and
       np.issubdtype(eeg.data.dtype, np.integer)):

        fname = ''.join([chr(x) for x in eeg.data])
        basedir = op.dirname(input_fname)
        data_fname = op.join(basedir, fname)
        if op.isfile(data_fname):
            return (True, fname)
        else:
            return (False, "")
    else:
        return (False, "")


def _check_mat_struct(fname):
    """Check if the mat struct contains 'EEG'."""
    if not check_version('scipy', '0.12'):
        raise RuntimeError('scipy >= 0.12 must be installed for reading EEGLAB'
                           ' files.')
    from scipy import io
    try:
        # Try to read old style Matlab file
        with open(fname, 'rb') as fhandle:
            mat = io.whosmat(fhandle, struct_as_record=False, squeeze_me=True)
    except NotImplementedError:
        # Try to read new style Matlab file
        try:
            import h5py
        except ImportError:
            raise RuntimeError('Reading v7+ MATLAB format .set',
                               'requires h5py, which could not',
                               'be imported')
        with h5py.File(fname) as f:
            mat = list(f.keys())
        if 'ALLEEG' in mat:
            mat[0] = u'ALLEEG'
        elif 'EEG' in mat:
            mat[0] = u'EEG'

    if 'ALLEEG' in mat[0]:
        raise NotImplementedError(
            'Loading an ALLEEG array is not supported. Please contact'
            'mne-python developers for more information.')
    elif 'EEG' not in mat[0]:
        msg = ('Unknown array in the .set file.')
        raise ValueError(msg)


def _to_loc(ll):
    """Check if location exists."""
    if isinstance(ll, (int, float)):
        # Numeric value
        return ll

    elif isinstance(ll, (list, tuple)) and len(ll) > 0:
        # Non-empty list or tuple
        # (Should elements be checked to ensure they're numeric?)
        return ll

    elif hasattr(ll, 'dtype') and \
        ((np.issubdtype(ll.dtype, np.integer) or
          np.issubdtype(ll.dtype, np.dtype(float).type))):
        # Numeric numpy array
        if isinstance(ll, np.ndarray):
            return list(ll) if ll.size > 0 else np.nan
        else:
            return ll
    else:
        return np.nan


def _get_info(eeg, montage, eog=()):
    """Get measurement info."""
    from scipy import io
    info = _empty_info(sfreq=eeg.srate)
    update_ch_names = True

    # add the ch_names and info['chs'][idx]['loc']
    path = None
    if not isinstance(eeg.chanlocs, np.ndarray) and eeg.nbchan == 1:
            eeg.chanlocs = [eeg.chanlocs]

    if len(eeg.chanlocs) > 0:
        pos_fields = ['X', 'Y', 'Z']

        # Assume data did not come from an hdf file unless proven so
        hdf5_flag = False

        if (isinstance(eeg.chanlocs, np.ndarray) and not isinstance(
                eeg.chanlocs[0], io.matlab.mio5_params.mat_struct)):
            try:
                has_pos = all(fld in eeg.chanlocs[0].dtype.names
                              for fld in pos_fields)
            except TypeError:
                # when stored as hdf, empty eeg.chanlocs is
                # array([0, 0], dtype=uint64)
                # which raises a Type Error, but we still need
                # default chan names
                ch_names = ["EEG %03d" % ii for ii in range(eeg.nbchan)]
                hdf5_flag = True
        else:
            has_pos = all(hasattr(eeg.chanlocs[0], fld)
                          for fld in pos_fields)
        if not hdf5_flag:
            # If type error thrown by, presumably, hdf5 file, skip the rest
            # of the if len(eeg.chanlocs) > 0 block
            get_pos = has_pos and montage is None
            pos_ch_names, ch_names, pos = list(), list(), list()
            kind = 'user_defined'
            update_ch_names = False
            for chanloc in eeg.chanlocs:
                ch_names.append(chanloc.labels)
                if get_pos:
                    loc_x = _to_loc(chanloc.X)
                    loc_y = _to_loc(chanloc.Y)
                    loc_z = _to_loc(chanloc.Z)
                    locs = np.r_[-loc_y, loc_x, loc_z]
                    if not np.any(np.isnan(locs)):
                        pos_ch_names.append(chanloc.labels)
                        pos.append(locs)
            n_channels_with_pos = len(pos_ch_names)
            info = create_info(ch_names, eeg.srate, ch_types='eeg')
            if n_channels_with_pos > 0:
                selection = np.arange(n_channels_with_pos)
                montage = Montage(np.array(pos), pos_ch_names, kind, selection)
    elif isinstance(montage, string_types):
        path = op.dirname(montage)
    else:  # if eeg.chanlocs is empty, we still need default chan names
        ch_names = ["EEG %03d" % ii for ii in range(eeg.nbchan)]

    if montage is None:
        info = create_info(ch_names, eeg.srate, ch_types='eeg')
    else:
        _check_update_montage(
            info, montage, path=path, update_ch_names=update_ch_names,
            raise_missing=False)

    info['buffer_size_sec'] = 1.  # reasonable default
    # update the info dict

    if eog == 'auto':
        eog = _find_channels(ch_names)

    for idx, ch in enumerate(info['chs']):
        ch['cal'] = CAL
        if ch['ch_name'] in eog or idx in eog:
            ch['coil_type'] = FIFF.FIFFV_COIL_NONE
            ch['kind'] = FIFF.FIFFV_EOG_CH
    return info


def read_raw_eeglab(input_fname, montage=None, eog=(), event_id=None,
                    event_id_func='strip_to_integer', preload=False,
                    verbose=None, uint16_codec=None):
    r"""Read an EEGLAB .set file.

    Parameters
    ----------
    input_fname : str
        Path to the .set file. If the data is stored in a separate .fdt file,
        it is expected to be in the same folder as the .set file.
    montage : str | None | instance of montage
        Path or instance of montage containing electrode positions.
        If None, sensor locations are (0,0,0). See the documentation of
        :func:`mne.channels.read_montage` for more information.
    eog : list | tuple | 'auto'
        Names or indices of channels that should be designated EOG channels.
        If 'auto', the channel names containing ``EOG`` or ``EYE`` are used.
        Defaults to empty tuple.
    event_id : dict | None
        The ids of the events to consider. If None (default), an empty dict is
        used and ``event_id_func`` (see below) is called on every event value.
        If dict, the keys will be mapped to trigger values on the stimulus
        channel and only keys not in ``event_id`` will be handled by
        ``event_id_func``. Keys are case-sensitive.
        Example::

            {'SyncStatus': 1; 'Pulse Artifact': 3}

    event_id_func : None | str | callable
        What to do for events not found in ``event_id``. Must take one ``str``
        argument and return an ``int``. If string, must be 'strip-to-integer',
        in which case it defaults to stripping event codes such as "D128" or
        "S  1" of their non-integer parts and returns the integer.
        If the event is not in the ``event_id`` and calling ``event_id_func``
        on it results in a ``TypeError`` (e.g. if ``event_id_func`` is
        ``None``) or a ``ValueError``, the event is dropped.
    preload : bool or str (default False)
        Preload data into memory for data manipulation and faster indexing.
        If True, the data will be preloaded into memory (fast, requires
        large amount of memory). If preload is a string, preload is the
        file name of a memory-mapped file which is used to store the data
        on the hard drive (slower, requires less memory). Note that
        preload=False will be effective only if the data is stored in a
        separate binary file.
    verbose : bool | str | int | None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more).
    uint16_codec : str | None
        If your \*.set file contains non-ascii characters, sometimes reading
        it may fail and give rise to error message stating that "buffer is
        too small". ``uint16_codec`` allows to specify what codec (for example:
        'latin1' or 'utf-8') should be used when reading character arrays and
        can therefore help you solve this problem.

    Returns
    -------
    raw : Instance of RawEEGLAB
        A Raw object containing EEGLAB .set data.

    Notes
    -----
    .. versionadded:: 0.11.0

    See Also
    --------
    mne.io.Raw : Documentation of attribute and methods.
    """
    return RawEEGLAB(input_fname=input_fname, montage=montage, preload=preload,
                     eog=eog, event_id=event_id, event_id_func=event_id_func,
                     verbose=verbose, uint16_codec=uint16_codec)


def read_epochs_eeglab(input_fname, events=None, event_id=None, montage=None,
                       eog=(), verbose=None, uint16_codec=None):
    r"""Reader function for EEGLAB epochs files.

    Parameters
    ----------
    input_fname : str
        Path to the .set file. If the data is stored in a separate .fdt file,
        it is expected to be in the same folder as the .set file.
    events : str | array, shape (n_events, 3) | None
        Path to events file. If array, it is the events typically returned
        by the read_events function. If some events don't match the events
        of interest as specified by event_id, they will be marked as 'IGNORED'
        in the drop log. If None, it is constructed from the EEGLAB (.set) file
        with each unique event encoded with a different integer.
    event_id : int | list of int | dict | None
        The id of the event to consider. If dict, the keys can later be used
        to access associated events.
        Example::

            {"auditory":1, "visual":3}

        If int, a dict will be created with
        the id as string. If a list, all events with the IDs specified
        in the list are used. If None, the event_id is constructed from the
        EEGLAB (.set) file with each descriptions copied from `eventtype`.
    montage : str | None | instance of montage
        Path or instance of montage containing electrode positions.
        If None, sensor locations are (0,0,0). See the documentation of
        :func:`mne.channels.read_montage` for more information.
    eog : list | tuple | 'auto'
        Names or indices of channels that should be designated EOG channels.
        If 'auto', the channel names containing ``EOG`` or ``EYE`` are used.
        Defaults to empty tuple.
    verbose : bool | str | int | None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more).
    uint16_codec : str | None
        If your \*.set file contains non-ascii characters, sometimes reading
        it may fail and give rise to error message stating that "buffer is
        too small". ``uint16_codec`` allows to specify what codec (for example:
        'latin1' or 'utf-8') should be used when reading character arrays and
        can therefore help you solve this problem.

    Returns
    -------
    epochs : instance of Epochs
        The epochs.

    Notes
    -----
    .. versionadded:: 0.11.0


    See Also
    --------
    mne.Epochs : Documentation of attribute and methods.
    """
    epochs = EpochsEEGLAB(input_fname=input_fname, events=events, eog=eog,
                          event_id=event_id, montage=montage, verbose=verbose,
                          uint16_codec=uint16_codec)
    return epochs


def _bunchify(mapping, name='BU'):
    """Convert mappings to Bunches recursively.

    Based on https://gist.github.com/hangtwenty/5960435.
    """
    if isinstance(mapping, Mapping):
        for key, value in list(mapping.items()):
            mapping[key] = _bunchify(value)
        return _bunch_wrapper(name, **mapping)
    elif isinstance(mapping, list):
        return [_bunchify(item) for item in mapping]
    return mapping


def _bunch_wrapper(name, **kwargs):
    """Convert mappings to Bunches."""
    return Bunch(**kwargs)


def _bunch_data_2_strs(bunch_data, field, lower=True):
    """ Takes string values stored as ascii values in numpy arrays
        in bunch objects and returns list of human-readable strings"""

    str_list = [''.join([chr(x) for x in curr_label.__dict__[field]]).strip()
                for curr_label in bunch_data]
    return str_list


def _bunch_str_conversions(bunch_data, str_conversion_fields):
    """ Converts selected fields from bunch object from 1D array
        aof ascii values to strs"""

    for curr_field in str_conversion_fields:
        c1 = (len(bunch_data) > 0)
        c2 = (c1 and (curr_field in bunch_data[0]))
        c3 = (c2 and
              not isinstance(bunch_data[0].__dict__[curr_field], str))
        if c3:
            str_data = _bunch_data_2_strs(bunch_data, curr_field)
            for ctr, curr_str in enumerate(str_data):
                bunch_data[ctr].__dict__[curr_field] = curr_str
    return bunch_data


def _bunch_derefs(orig, bunch_data, deref_fields):
    """ Dereferences h5py.h5r.Reference objects. Ensures that each
        field of bunch object with dereferenced objects stores them
        in a list, even if that list has only 1 element. """
    try:
        import h5py
    except ImportError:
        raise RuntimeError('Reading v7+ MATLAB format .set',
                           'requires h5py, which could not',
                           'be imported')

    for curr_field in deref_fields:
        bd = bunch_data[0].__dict__[curr_field]
        c1 = (len(bunch_data) > 0)
        c2 = (curr_field in bunch_data[0])
        c3 = (len(bd) > 0)
        c4 = (isinstance(bd[0], h5py.h5r.Reference))
        if (c1 and c2 and c3 and c4):
            for ctr in range(len(bunch_data)):
                bd = bunch_data[ctr].__dict__[curr_field]
                try:
                    # Ensure bunch_data[ctr].__dict__[curr_field] is iterable
                    # before attempting to iterate over it
                    iter(bd)
                except TypeError:
                    deref = [orig[bd].value.flatten()]
                else:
                    deref = [orig[x].value.flatten() for x in bd]

                bunch_data[ctr].__dict__[curr_field] = deref

    return bunch_data


def _get_hdf_eeg_data(input_fname):

    try:
        import h5py
    except ImportError:
        raise RuntimeError('Reading v7+ MATLAB format .set',
                           'requires h5py, which could not',
                           'be imported')

    logger.info("Attempting to read Matlab style hdf file")
    with h5py.File(input_fname) as f:
        eeg_dict = hdf_2_dict(f, f['EEG'], parent=None)
    eeg = _bunchify(eeg_dict)
    ascii_check = _check_for_ascii_filename(eeg, input_fname)
    if ascii_check[0]:
        eeg.data = ascii_check[1]
    else:
        eeg.data = eeg.data.transpose()

    return eeg


def hdf_2_dict(orig, in_hdf, parent=None, indent=''):
    """Convert h5py obj to dict."""
    try:
        import h5py
    except ImportError:
        raise RuntimeError('Reading v7+ MATLAB format .set',
                           'requires h5py, which could not',
                           'be imported')
    out_dict = {}
    variable_names = in_hdf.keys()
    indent_incr = '    '

    for curr in sorted(variable_names):
        if parent is None:
            curr_name = curr
        else:
            curr_name = '_'.join([parent, curr])

        msg = indent + "Converting " + curr_name
        if isinstance(in_hdf[curr], h5py.Dataset):
            suffix = " - Dataset"
            logger.debug(msg + suffix)
            temp = in_hdf[curr].value
            if 1 in temp.shape:
                temp = temp.flatten()

            if isinstance(temp[0], h5py.h5r.Reference):
                temp = np.array([orig[x].value.flatten()[0] for x in temp])

            if len(temp) == 1:
                temp = np.asscalar(temp[0])
                if isinstance(temp, float) and temp.is_integer():
                    temp = int(temp)

            out_dict[curr] = temp

        elif isinstance(in_hdf[curr], h5py.Group):
            suffix = " - Group"
            logger.debug(msg + suffix)

            if curr == 'chanlocs':
                temp = _hlGroup_2_bunch_list(orig, in_hdf[curr], curr,
                                             indent + indent_incr)
                # For some reason an empty chanloc field, which is stored as
                # [] <type 'numpy.ndarray'> in Matlab's original set file
                # becomes array([0, 0], dtype=uint64) when Matlab
                # stores as HDF5 (!?)
                # Since chanloc's values all appear to be scalars or strings,
                # each value of array[0,0] will be replaced by [].

                temp = [{curr_key: np.array([])
                         if np.array_equal(curr_dict[curr_key],
                                           np.array([0, 0], dtype=np.uint64))
                         else curr_dict[curr_key]
                         for curr_key in curr_dict}
                        for curr_dict in temp]

                # Rebunchify temp
                temp = [Bunch(**x) for x in temp]

                # TO DO add tests to know when to add
                # these (& other) string fields
                str_conversion_fields = ('type', 'labels')
                temp = _bunch_str_conversions(temp, str_conversion_fields)

            elif curr == 'event':
                temp = _hlGroup_2_bunch_list(orig, in_hdf[curr], curr,
                                             indent + indent_incr)

                # TO DO add tests to know when to add
                # these (& other) string fields
                str_conversion_fields = ('type', 'usertags')
                temp = _bunch_str_conversions(temp, str_conversion_fields)
                temp = np.asarray(temp)

            elif curr == 'epoch':
                temp = _hlGroup_2_bunch_list(orig, in_hdf[curr],
                                             curr_name, indent + indent_incr)

                deref_fields = ('eventtype', 'eventlatency', 'eventurevent',
                                'eventduration', 'eventvalue')
                temp = _bunch_derefs(orig, temp, deref_fields)

                for curr_elem in temp:
                    eventtype_str = [''.join([chr(x) for x in c_evt])
                                     for c_evt in curr_elem.eventtype]
                    curr_elem.eventtype = np.asarray(eventtype_str)

            else:
                temp = hdf_2_dict(orig, in_hdf[curr],
                                  curr_name, indent + indent_incr)
            out_dict[curr] = temp

        else:
            sys.exit("Unknown type")

    return out_dict


def _hlGroup_2_bunch_list(orig, in_hlGroup, tuple_name, indent):
    """Returns list of Bunch objects - A Bunch object is a
       dictionary-like object that exposes its keys as attributes.
       ASSUMES: The group consists solely of arrays of HDF5 obj refs,
       and that these refs all reference 2D numpy arrays that need to
       be flattened to either 1D arrays or Scalars"""

    try:
        import h5py
    except ImportError:
        raise RuntimeError('Reading v7+ MATLAB format .set',
                           'requires h5py, which could not',
                           'be imported')

    try:
        # h5_values gives dict of 1D arrays of HDF obj references
        h5_values = {ct: in_hlGroup[ct].value.flatten() for ct in in_hlGroup}

        # derefs dereferences HDF obj references and cnverts arrays with
        # shapes = (1,) to scalars. Returns adict mapping keys to lists
        # of arrays and scalars
        derefs = {x: [orig[y].value.flatten()
                      if orig[y].value.flatten().shape != (1,)
                      else orig[y].value.flatten()[0]
                      for y in h5_values[x]]
                  if isinstance(h5_values[x], np.ndarray) and
                  isinstance(h5_values[x][0], h5py.Reference)
                  else h5_values[x]
                  for x in h5_values}

        for ct in in_hlGroup:
            msg = indent + "Converting " + tuple_name + '_' + ct
            logger.debug(msg)

    except IOError:
        derefs = {ct: [None] for ct in in_hlGroup}
        warn("Couldn't read", tuple_name, ". Assuming empty")

    sz = len(derefs[list(derefs.keys())[0]])
    bch_list = [Bunch(**{key: derefs[key][x] for key in derefs})
                for x in range(sz)]
    return bch_list


def _get_eeg_data(input_fname, uint16_codec=None):
    from scipy import io
    try:
        # Try to read old style Matlab file
        with open(input_fname, 'rb') as fhandle:
            eeg = io.loadmat(fhandle, struct_as_record=False,
                             squeeze_me=True,
                             uint16_codec=uint16_codec)['EEG']
    except NotImplementedError:
        # Try to read new style Matlab file (Version 7.3+)
        # Note: Now eeg will be returned as a Bunch object,
        # instead of an io.matlab.mio5_params.mat_struct object.
        eeg = _get_hdf_eeg_data(input_fname)
        str_conversion_fields = ('datfile', 'filename', 'filepath',
                                 'history', 'ref', 'saved', 'setname')
        for curr_field in str_conversion_fields:
            temp = eeg[curr_field]
            c1 = isinstance(temp, np.ndarray)
            c2 = c1 and np.issubdtype(temp.dtype, np.integer)
            c3 = c2 and (min(temp) >= 0)
            if c3:
                eeg[curr_field] = ''.join([chr(y) for y in temp])

    return eeg


class RawEEGLAB(BaseRaw):
    r"""Raw object from EEGLAB .set file.

    Parameters
    ----------
    input_fname : str
        Path to the .set file. If the data is stored in a separate .fdt file,
        it is expected to be in the same folder as the .set file.
    montage : str | None | instance of montage
        Path or instance of montage containing electrode positions. If None,
        sensor locations are (0,0,0). See the documentation of
        :func:`mne.channels.read_montage` for more information.
    eog : list | tuple | 'auto'
        Names or indices of channels that should be designated EOG channels.
        If 'auto', the channel names containing ``EOG`` or ``EYE`` are used.
        Defaults to empty tuple.
    event_id : dict | None
        The ids of the events to consider. If None (default), an empty dict is
        used and ``event_id_func`` (see below) is called on every event value.
        If dict, the keys will be mapped to trigger values on the stimulus
        channel and only keys not in ``event_id`` will be handled by
        ``event_id_func``. Keys are case-sensitive.
        Example::

            {'SyncStatus': 1; 'Pulse Artifact': 3}

    event_id_func : None | str | callable
        What to do for events not found in ``event_id``. Must take one ``str``
        argument and return an ``int``. If string, must be 'strip-to-integer',
        in which case it defaults to stripping event codes such as "D128" or
        "S  1" of their non-integer parts and returns the integer.
        If the event is not in the ``event_id`` and calling ``event_id_func``
        on it results in a ``TypeError`` (e.g. if ``event_id_func`` is
        ``None``) or a ``ValueError``, the event is dropped.
    preload : bool or str (default False)
        Preload data into memory for data manipulation and faster indexing.
        If True, the data will be preloaded into memory (fast, requires large
        amount of memory). If preload is a string, preload is the file name of
        a memory-mapped file which is used to store the data on the hard
        drive (slower, requires less memory).
    verbose : bool | str | int | None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more).
    uint16_codec : str | None
        If your \*.set file contains non-ascii characters, sometimes reading
        it may fail and give rise to error message stating that "buffer is
        too small". ``uint16_codec`` allows to specify what codec (for example:
        'latin1' or 'utf-8') should be used when reading character arrays and
        can therefore help you solve this problem.

    Returns
    -------
    raw : Instance of RawEEGLAB
        A Raw object containing EEGLAB .set data.

    Notes
    -----
    .. versionadded:: 0.11.0

    See Also
    --------
    mne.io.Raw : Documentation of attribute and methods.
    """

    @verbose
    def __init__(self, input_fname, montage, eog=(), event_id=None,
                 event_id_func='strip_to_integer', preload=False,
                 verbose=None, uint16_codec=None):  # noqa: D102

        basedir = op.dirname(input_fname)
        _check_mat_struct(input_fname)
        eeg = _get_eeg_data(input_fname, uint16_codec)

        if eeg.trials != 1:
            raise TypeError('The number of trials is %d. It must be 1 for raw'
                            ' files. Please use `mne.io.read_epochs_eeglab` if'
                            ' the .set file contains epochs.' % eeg.trials)

        last_samps = [eeg.pnts - 1]
        info = _get_info(eeg, montage, eog=eog)

        stim_chan = dict(ch_name='STI 014', coil_type=FIFF.FIFFV_COIL_NONE,
                         kind=FIFF.FIFFV_STIM_CH, logno=len(info["chs"]) + 1,
                         scanno=len(info["chs"]) + 1, cal=1., range=1.,
                         loc=np.full(12, np.nan), unit=FIFF.FIFF_UNIT_NONE,
                         unit_mul=0., coord_frame=FIFF.FIFFV_COORD_UNKNOWN)
        info['chs'].append(stim_chan)
        info._update_redundant()

        events = read_events_eeglab(eeg, event_id=event_id,
                                    event_id_func=event_id_func)
        self._create_event_ch(events, n_samples=eeg.pnts)

        if isinstance(eeg.data, string_types):
            data_fname = op.join(basedir, eeg.data)
            _check_fname(data_fname)
            logger.info('Reading %s' % data_fname)

            super(RawEEGLAB, self).__init__(
                info, preload, filenames=[data_fname], last_samps=last_samps,
                orig_format='double', verbose=verbose)
        else:
            if preload is False or isinstance(preload, string_types):
                warn('Data will be preloaded. preload=False or a string '
                     'preload is not supported when the data is stored in '
                     'the .set file')
            # can't be done in standard way with preload=True because of
            # different reading path (.set file)

            if eeg.nbchan == 1 and len(eeg.data.shape) == 1:
                n_chan, n_times = [1, eeg.data.shape[0]]
            else:
                n_chan, n_times = eeg.data.shape

            data = np.empty((n_chan + 1, n_times), dtype=np.double)
            data[:-1] = eeg.data
            data *= CAL
            data[-1] = self._event_ch
            super(RawEEGLAB, self).__init__(
                info, data, last_samps=last_samps, orig_format='double',
                verbose=verbose)

    def _create_event_ch(self, events, n_samples=None):
        """Create the event channel."""
        n_dropped = len(events[:, 0]) - len(set(events[:, 0]))
        if n_dropped > 0:
            warn(str(n_dropped) + " events will be dropped because they "
                 "occur on the same time sample as another event. "
                 "`mne.io.Raw` objects store events on an event channel, "
                 "which cannot represent two events on the same sample. You "
                 "can extract the original event structure using "
                 "`mne.io.eeglab.read_events_eeglab`. Then, you can e.g. "
                 "subset the extracted events for constructing epochs.")
        if n_samples is None:
            n_samples = self.last_samp - self.first_samp + 1
        events = np.array(events, int)
        if events.ndim != 2 or events.shape[1] != 3:
            raise ValueError("[n_events x 3] shaped array required")
        # update events
        self._event_ch = _synthesize_stim_channel(events, n_samples)

    def _read_segment_file(self, data, idx, fi, start, stop, cals, mult):
        """Read a chunk of raw data."""
        _read_segments_file(self, data, idx, fi, start, stop, cals, mult,
                            dtype=np.float32, trigger_ch=self._event_ch,
                            n_channels=self.info['nchan'] - 1)


class EpochsEEGLAB(BaseEpochs):
    r"""Epochs from EEGLAB .set file.

    Parameters
    ----------
    input_fname : str
        Path to the .set file. If the data is stored in a separate .fdt file,
        it is expected to be in the same folder as the .set file.
    events : str | array, shape (n_events, 3) | None
        Path to events file. If array, it is the events typically returned
        by the read_events function. If some events don't match the events
        of interest as specified by event_id, they will be marked as 'IGNORED'
        in the drop log. If None, it is constructed from the EEGLAB (.set) file
        with each unique event encoded with a different integer.
    event_id : int | list of int | dict | None
        The id of the event to consider. If dict,
        the keys can later be used to access associated events. Example:
        dict(auditory=1, visual=3). If int, a dict will be created with
        the id as string. If a list, all events with the IDs specified
        in the list are used. If None, the event_id is constructed from the
        EEGLAB (.set) file with each descriptions copied from ``eventtype``.
    tmin : float
        Start time before event.
    baseline : None or tuple of length 2 (default (None, 0))
        The time interval to apply baseline correction.
        If None do not apply it. If baseline is (a, b)
        the interval is between "a (s)" and "b (s)".
        If a is None the beginning of the data is used
        and if b is None then b is set to the end of the interval.
        If baseline is equal to (None, None) all the time
        interval is used.
        The baseline (a, b) includes both endpoints, i.e. all
        timepoints t such that a <= t <= b.
    reject : dict | None
        Rejection parameters based on peak-to-peak amplitude.
        Valid keys are 'grad' | 'mag' | 'eeg' | 'eog' | 'ecg'.
        If reject is None then no rejection is done. Example::

            reject = dict(grad=4000e-13, # T / m (gradiometers)
                          mag=4e-12, # T (magnetometers)
                          eeg=40e-6, # V (EEG channels)
                          eog=250e-6 # V (EOG channels)
                          )
    flat : dict | None
        Rejection parameters based on flatness of signal.
        Valid keys are 'grad' | 'mag' | 'eeg' | 'eog' | 'ecg', and values
        are floats that set the minimum acceptable peak-to-peak amplitude.
        If flat is None then no rejection is done.
    reject_tmin : scalar | None
        Start of the time window used to reject epochs (with the default None,
        the window will start with tmin).
    reject_tmax : scalar | None
        End of the time window used to reject epochs (with the default None,
        the window will end with tmax).
    montage : str | None | instance of montage
        Path or instance of montage containing electrode positions.
        If None, sensor locations are (0,0,0). See the documentation of
        :func:`mne.channels.read_montage` for more information.
    eog : list | tuple | 'auto'
        Names or indices of channels that should be designated EOG channels.
        If 'auto', the channel names containing ``EOG`` or ``EYE`` are used.
        Defaults to empty tuple.
    verbose : bool | str | int | None
        If not None, override default verbose level (see :func:`mne.verbose`
        and :ref:`Logging documentation <tut_logging>` for more).
    uint16_codec : str | None
        If your \*.set file contains non-ascii characters, sometimes reading
        it may fail and give rise to error message stating that "buffer is
        too small". ``uint16_codec`` allows to specify what codec (for example:
        'latin1' or 'utf-8') should be used when reading character arrays and
        can therefore help you solve this problem.

    Notes
    -----
    .. versionadded:: 0.11.0

    See Also
    --------
    mne.Epochs : Documentation of attribute and methods.
    """

    @verbose
    def __init__(self, input_fname, events=None, event_id=None, tmin=0,
                 baseline=None, reject=None, flat=None, reject_tmin=None,
                 reject_tmax=None, montage=None, eog=(), verbose=None,
                 uint16_codec=None):  # noqa: D102
        _check_mat_struct(input_fname)
        eeg = _get_eeg_data(input_fname, uint16_codec)

        if not ((events is None and event_id is None) or
                (events is not None and event_id is not None)):
            raise ValueError('Both `events` and `event_id` must be '
                             'None or not None')

        if events is None and eeg.trials > 1:
            # first extract the events and construct an event_id dict
            event_name, event_latencies, unique_ev = list(), list(), list()
            ev_idx = 0
            warn_multiple_events = False

            for ep in eeg.epoch:
                if isinstance(ep.eventtype, int):
                    ep.eventtype = str(ep.eventtype)
                if not isinstance(ep.eventtype, string_types):
                    event_type = '/'.join([str(et) for et
                                           in ep.eventtype.tolist()])
                    event_name.append(event_type)
                    # store latency of only first event
                    event_latencies.append(eeg.event[ev_idx].latency)
                    ev_idx += len(ep.eventtype)
                    warn_multiple_events = True
                else:
                    event_type = ep.eventtype
                    event_name.append(ep.eventtype)
                    event_latencies.append(eeg.event[ev_idx].latency)
                    ev_idx += 1

                if event_type not in unique_ev:
                    unique_ev.append(event_type)

                # invent event dict but use id > 0 so you know its a trigger
                event_id = dict((ev, idx + 1) for idx, ev
                                in enumerate(unique_ev))

            # warn about multiple events in epoch if necessary
            if warn_multiple_events:
                warn('At least one epoch has multiple events. Only the latency'
                     ' of the first event will be retained.')

            # now fill up the event array
            events = np.zeros((eeg.trials, 3), dtype=int)
            for idx in range(0, eeg.trials):
                if idx == 0:
                    prev_stim = 0
                elif (idx > 0 and
                        event_latencies[idx] - event_latencies[idx - 1] == 1):
                    prev_stim = event_id[event_name[idx - 1]]
                events[idx, 0] = event_latencies[idx]
                events[idx, 1] = prev_stim
                events[idx, 2] = event_id[event_name[idx]]
        elif isinstance(events, string_types):
            events = read_events(events)

        logger.info('Extracting parameters from %s...' % input_fname)
        input_fname = op.abspath(input_fname)
        info = _get_info(eeg, montage, eog=eog)

        for key, val in event_id.items():
            if val not in events[:, 2]:
                raise ValueError('No matching events found for %s '
                                 '(event id %i)' % (key, val))

        # Read data
        if isinstance(eeg, Bunch):
            # if hdf5 data was read in, then file name might be encoded
            # as array of ascii values of characters
            ascii_check = _check_for_ascii_filename(eeg, input_fname)
            if ascii_check[0]:
                eeg.data = ascii_check[1]

        if isinstance(eeg.data, string_types):
            basedir = op.dirname(input_fname)
            data_fname = op.join(basedir, eeg.data)
            _check_fname(data_fname)
            with open(data_fname, 'rb') as data_fid:
                data = np.fromfile(data_fid, dtype=np.float32)
                data = data.reshape((eeg.nbchan, eeg.pnts, eeg.trials),
                                    order="F")
        else:
            data = eeg.data

        if eeg.nbchan == 1 and len(data.shape) == 2:
            data = data[np.newaxis, :]
        data = data.transpose((2, 0, 1)).astype('double')
        data *= CAL

        assert data.shape == (eeg.trials, eeg.nbchan, eeg.pnts)
        tmin, tmax = eeg.xmin, eeg.xmax

        super(EpochsEEGLAB, self).__init__(
            info, data, events, event_id, tmin, tmax, baseline,
            reject=reject, flat=flat, reject_tmin=reject_tmin,
            reject_tmax=reject_tmax, filename=input_fname, verbose=verbose)

        # data are preloaded but _bad_dropped is not set so we do it here:
        self._bad_dropped = True
        logger.info('Ready.')


def read_events_eeglab(eeg, event_id=None, event_id_func='strip_to_integer',
                       uint16_codec=None):
    r"""Create events array from EEGLAB structure.

    An event array is constructed by looking up events in the
    event_id, trying to reduce them to their integer part otherwise, and
    entirely dropping them (with a warning) if this is impossible.
    Returns a 1x3 array of zeros if no events are found.

    Usually, the EEGLAB readers will automatically construct event information
    for you. However, the reader for continuous data stores event information
    in the stimulus channel, which can only code one event per time sample.
    Use this function if your EEGLAB file has events happening at the
    same time (sample) point to manually create an events array.

    Parameters
    ----------
    eeg : str | object
        The EEGLAB object from which events are read in.
        If str, path to the (EEGLAB) .set file.
        Else, the "EEG" field of a MATLAB EEGLAB structure as read in by
        scipy.io.loadmat.
    event_id : dict | None
        The ids of the events to consider. If None (default), an empty dict is
        used and ``event_id_func`` (see below) is called on every event value.
        If dict, the keys will be mapped to trigger values on the stimulus
        channel and only keys not in ``event_id`` will be handled by
        ``event_id_func``. Keys are case-sensitive.
        Example::

            {'SyncStatus': 1; 'Pulse Artifact': 3}

    event_id_func : None | str | callable
        What to do for events not found in ``event_id``. Must take one ``str``
        argument and return an ``int``. If string, must be 'strip-to-integer',
        in which case it defaults to stripping event codes such as "D128" or
        "S  1" of their non-integer parts and returns the integer.
        If the event is not in the ``event_id`` and calling ``event_id_func``
        on it results in a ``TypeError`` (e.g. if ``event_id_func`` is
        ``None``) or a ``ValueError``, the event is dropped.
    uint16_codec : str | None
        If your \*.set file contains non-ascii characters, sometimes reading
        it may fail and give rise to error message stating that "buffer is
        too small". ``uint16_codec`` allows to specify what codec (for example:
        'latin1' or 'utf-8') should be used when reading character arrays and
        can therefore help you solve this problem.

    Returns
    -------
    events : array, shape = (n_events, 3)
        All events that were found. The first column contains the event time
        in samples and the third column contains the event id. The center
        column is zero.

    See Also
    --------
    mne.find_events : Extract events from a stim channel. Note that stim
        channels can only code for one event per time point.
    """
    if event_id_func is 'strip_to_integer':
        event_id_func = _strip_to_integer
    if event_id is None:
        event_id = dict()

    if isinstance(eeg, string_types):
        from scipy import io

        try:
            with open(eeg, 'rb') as fhandle:
                eeg = io.loadmat(fhandle, struct_as_record=False,
                                 squeeze_me=True,
                                 uint16_codec=uint16_codec)['EEG']
        except NotImplementedError:
            # Try to read new style Matlab file (Version 7.3+)
            eeg = _get_hdf_eeg_data(eeg)

    annotations = _read_annotations_eeglab(eeg)
    types = annotations.description
    latencies = annotations.onset

    if "boundary" in types and "boundary" not in event_id:
        warn("The data contains 'boundary' events, indicating data "
             "discontinuities. Be cautious of filtering and epoching around "
             "these events.")

    if len(types) < 1:  # if there are 0 events, we can exit here
        logger.info('No events found, returning empty stim channel ...')
        return np.zeros((0, 3))

    if (latencies < 0).any():
        raise ValueError('At least one event sample index is negative. Please'
                         ' check if EEG.event.sample values are correct.')

    not_in_event_id = set(x for x in types if x not in event_id)
    not_purely_numeric = set(x for x in not_in_event_id if not x.isdigit())
    no_numbers = set([x for x in not_purely_numeric
                      if not any([d.isdigit() for d in x])])
    have_integers = set([x for x in not_purely_numeric
                         if x not in no_numbers])
    if len(not_purely_numeric) > 0:
        basewarn = "Events like the following will be dropped"
        n_no_numbers, n_have_integers = len(no_numbers), len(have_integers)
        if n_no_numbers > 0:
            no_num_warm = " entirely: {0}, {1} in total"
            warn(basewarn + no_num_warm.format(list(no_numbers)[:5],
                                               n_no_numbers))
        if n_have_integers > 0 and event_id_func is None:
            intwarn = (", but could be reduced to their integer part "
                       "instead with the default `event_id_func`: "
                       "{0}, {1} in total")
            warn(basewarn + intwarn.format(list(have_integers)[:5],
                                           n_have_integers))

    events = list()
    for tt, latency in zip(types, latencies):
        try:  # look up the event in event_id and if not, try event_id_func
            event_code = event_id[tt] if tt in event_id else event_id_func(tt)
            events.append([int(latency), 1, event_code])
        except (ValueError, TypeError):  # if event_id_func fails
            pass  # We're already raising warnings above, so we just drop

    if len(events) < len(types):
        missings = len(types) - len(events)
        msg = ("{0}/{1} event codes could not be mapped to integers. Use "
               "the 'event_id' parameter to map such events manually.")
        warn(msg.format(missings, len(types)))
        if len(events) < 1:
            warn("As is, the trigger channel will consist entirely of zeros.")
            return np.zeros((0, 3))

    return np.asarray(events)


def _read_annotations_eeglab(eeg):
    if not hasattr(eeg, 'event'):
        onset = []
        duration = []
        description = []
    elif isinstance(eeg.event, np.ndarray):
        description = [str(event.type) for event in eeg.event]
        onset = [event.latency - 1 for event in eeg.event]
        if (len(onset) > 0) and hasattr(eeg.event[0], 'duration'):
            duration = [event.duration for event in eeg.event]
        else:
            duration = np.zeros(len(onset))
    else:
        # only one event - TypeError: 'mat_struct' object is not iterable
        description = [str(eeg.event.type)]
        onset = [eeg.event.latency - 1]
        duration = getattr(eeg.event, 'duration', np.zeros(1))

    return Annotations(onset=onset, duration=duration, description=description)


def read_annotations_eeglab(fname, uint16_codec=None):
    r"""Create Annotations from EEGLAB file.

    This function reads the event attribute from the EEGLAB
    structure and makes an :class:`mne.Annotations` object.

    Parameters
    ----------
    fname : str | object
        The path to the (EEGLAB) .set file.
    uint16_codec : str | None
        If your \*.set file contains non-ascii characters, sometimes reading
        it may fail and give rise to error message stating that "buffer is
        too small". ``uint16_codec`` allows to specify what codec (for example:
        'latin1' or 'utf-8') should be used when reading character arrays and
        can therefore help you solve this problem.

    Returns
    -------
    annotations : instance of Annotations
        The annotations present in the file.
    """
    eeg = _get_eeg_data(fname, uint16_codec)
    return _read_annotations_eeglab(eeg)


def _strip_to_integer(trigger):
    """Return only the integer part of a string."""
    return int("".join([x for x in trigger if x.isdigit()]))
