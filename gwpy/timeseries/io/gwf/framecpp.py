# -*- coding: utf-8 -*-
# Copyright (C) Duncan Macleod (2013)
#
# This file is part of GWpy.
#
# GWpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GWpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GWpy.  If not, see <http://www.gnu.org/licenses/>.

"""Read data from gravitational-wave frame (GWF) files using
|LDAStools.frameCPP|__.
"""

from __future__ import division

import re
from math import ceil

import numpy

from LDAStools import frameCPP

from ....io import gwf as io_gwf
from ....io.cache import file_list
from ....segments import Segment
from ....time import LIGOTimeGPS
from ... import TimeSeries

from . import channel_dict_kwarg

__author__ = 'Duncan Macleod <duncan.macleod@ligo.org>'

FRAME_LIBRARY = 'LDAStools.frameCPP'

# error regexs
FRERR_NO_FRAME_AT_NUM = re.compile(
    r'\ARequest for frame (?P<frnum>\d+) exceeds the range of '
    r'0 through (?P<nframes>\d+)\Z')

# get frameCPP type mapping
NUMPY_TYPE_FROM_FRVECT = {
    frameCPP.FrVect.FR_VECT_C: numpy.int8,
    frameCPP.FrVect.FR_VECT_2S: numpy.int16,
    frameCPP.FrVect.FR_VECT_4S: numpy.int32,
    frameCPP.FrVect.FR_VECT_8S: numpy.int64,
    frameCPP.FrVect.FR_VECT_4R: numpy.float32,
    frameCPP.FrVect.FR_VECT_8R: numpy.float64,
    frameCPP.FrVect.FR_VECT_8C: numpy.complex64,
    frameCPP.FrVect.FR_VECT_16C: numpy.complex128,
    frameCPP.FrVect.FR_VECT_STRING: numpy.string_,
    frameCPP.FrVect.FR_VECT_1U: numpy.uint8,
    frameCPP.FrVect.FR_VECT_2U: numpy.uint16,
    frameCPP.FrVect.FR_VECT_4U: numpy.uint32,
    frameCPP.FrVect.FR_VECT_8U: numpy.uint64,
}

FRVECT_TYPE_FROM_NUMPY = dict(
    (v, k) for k, v in NUMPY_TYPE_FROM_FRVECT.items())


class _Skip(ValueError):
    """Error denoting that the contents of a given structure aren't required
    """
    pass


# -- read ---------------------------------------------------------------------

def read(source, channels, start=None, end=None, type=None,
         series_class=TimeSeries):
    # pylint: disable=redefined-builtin
    """Read a dict of series from one or more GWF files

    Parameters
    ----------
    source : `str`, `list`
        Source of data, any of the following:

        - `str` path of single data file,
        - `str` path of cache file,
        - `list` of paths.

    channels : `~gwpy.detector.ChannelList`, `list`
        a list of channels to read from the source.

    start : `~gwpy.time.LIGOTimeGPS`, `float`, `str` optional
        GPS start time of required data, anything parseable by
        :func:`~gwpy.time.to_gps` is fine.

    end : `~gwpy.time.LIGOTimeGPS`, `float`, `str`, optional
        GPS end time of required data, anything parseable by
        :func:`~gwpy.time.to_gps` is fine.

    type : `dict`, optional
        a `dict` of ``(name, channel-type)`` pairs, where ``channel-type``
        can be one of ``'adc'``, ``'proc'``, or ``'sim'``.

    series_class : `type`, optional
        the `Series` sub-type to return.

    Returns
    -------
    data : `~gwpy.timeseries.TimeSeriesDict` or similar
        a dict of ``(channel, series)`` pairs read from the GWF source(s).
    """
    # parse input source
    source = file_list(source)

    # parse type
    ctype = channel_dict_kwarg(type, channels, (str,))

    # read each individually and append
    out = series_class.DictClass()
    for i, file_ in enumerate(source):
        if i == 1:  # force data into fresh memory so that append works
            for name in out:
                out[name] = numpy.require(out[name], requirements=['O'])
        # read frame
        out.append(read_gwf(file_, channels, start=start, end=end,
                            ctype=ctype, series_class=series_class),
                   copy=False)
    return out


def read_gwf(filename, channels, start=None, end=None, ctype=None,
             series_class=TimeSeries):
    """Read a dict of series data from a single GWF file

    Parameters
    ----------
    filename : `str`
        the GWF path from which to read

    channels : `~gwpy.detector.ChannelList`, `list`
        a list of channels to read from the source.

    start : `~gwpy.time.LIGOTimeGPS`, `float`, `str` optional
        GPS start time of required data, anything parseable by
        :func:`~gwpy.time.to_gps` is fine.

    end : `~gwpy.time.LIGOTimeGPS`, `float`, `str`, optional
        GPS end time of required data, anything parseable by
        :func:`~gwpy.time.to_gps` is fine.

    type : `dict`, optional
        a `dict` of ``(name, channel-type)`` pairs, where ``channel-type``
        can be one of ``'adc'``, ``'proc'``, or ``'sim'``.

    series_class : `type`, optional
        the `Series` sub-type to return.

    Returns
    -------
    data : `~gwpy.timeseries.TimeSeriesDict` or similar
        a dict of ``(channel, series)`` pairs read from the GWF file.
    """
    # parse kwargs
    if not start:
        start = 0
    if not end:
        end = 0
    span = Segment(start, end)

    # open file
    stream = io_gwf.open_gwf(filename, 'r')
    nframes = stream.GetNumberOfFrames()

    # get type for all channels
    ctype = get_channel_types(stream, channels, ctype=ctype)

    # find channels
    out = series_class.DictClass()

    # loop over frames in GWF
    i = 0
    while True:
        this = i
        i += 1

        # read frame
        try:
            frame = stream.ReadFrameN(this)
        except IndexError:
            if this >= nframes:
                break
            raise

        # check whether we need this frame at all
        if not _need_frame(frame, start, end):
            continue

        # get epoch for this frame
        epoch = LIGOTimeGPS(*frame.GetGTime())

        # and read all the channels
        for channel in channels:
            try:
                new = _read_channel(stream, this, str(channel), ctype[channel],
                                    epoch, start, end,
                                    series_class=series_class)
            except _Skip:  # don't need this frame for this channel
                continue
            try:
                out[channel].append(new)
            except KeyError:
                out[channel] = numpy.require(new, requirements=['O'])

        # if we have all of the data we want, stop now
        if all(span in out[channel].span for channel in out):
            break

    # if any channels weren't read, something went wrong
    for channel in channels:
        if channel not in out:
            msg = "Failed to read {0!r} from {1!r}".format(
                str(channel), filename)
            if start or end:
                msg += ' for {0}'.format(span)
            raise ValueError(msg)

    return out


def _read_channel(stream, num, name, ctype, epoch, start, end,
                  series_class=TimeSeries):
    """Read a channel from a specific frame in a stream
    """
    _reader = getattr(stream, 'ReadFr{0}Data'.format(ctype.title()))
    data = _reader(num, name)
    return read_frdata(data, epoch, start, end, name=name,
                       series_class=series_class)


def get_channel_types(stream, channels, ctype=None):
    """Determine the frame data type for all channels

    Parameters
    ----------
    stream : `~LDAStools.frameCPP.IFrameFStream`
        the frame stream to read

    channels : `list` of `str`
        the list of channel names to find

    ctype : `dict`, optional
        ``(name, type)`` pairs for known channel types

    Returns
    -------
    ctype : `dict`
        the input `ctype` dict with types for all channels
    """
    ctype = ctype or {}

    toclist = {}  # only get names once
    toc = None  # only get TOC once

    for channel in channels:
        name = str(channel)
        # if ctype not declared, find it from the table-of-contents
        if not ctype.get(channel, None):
            toc = toc or stream.GetTOC()
            for typename in ['Sim', 'Proc', 'ADC']:
                if typename not in toclist:
                    toclist[typename] = getattr(toc,
                                                'Get{0}'.format(typename))()
                if name in toclist[typename]:
                    ctype[channel] = typename.lower()
                    break
        # if still not found, channel isn't in the frame
        if not ctype.get(channel, None):
            raise ValueError(
                "Channel {0} not found in table of contents".format(name))
    return ctype


def _need_frame(frame, start, end):
    frstart = LIGOTimeGPS(*frame.GetGTime())
    if end and frstart >= end:
        return False

    frend = frstart + frame.GetDt()
    if start and frend <= start:
        return False

    return True


def read_frdata(frdata, epoch, start, end, name=None, series_class=TimeSeries):
    """Read a series from an `FrData` structure

    Parameters
    ----------
    frdata : `LDAStools.frameCPP.FrAdcData` or similar
        the data structure to read

    epoch : `float`
        the GPS start time of the containing frame
        (`LDAStools.frameCPP.FrameH.GTime`)

    start : `float`
        the GPS start time of the user request

    end : `float`
        the GPS end time of the user request

    name : `str`, optional
        the name of the desired dataset, required to filter out
        unrelated `FrVect` structures

    series_class : `type`, optional
        the `Series` sub-type to return.

    Returns
    -------
    series : `~gwpy.timeseries.TimeSeriesBase`
        the formatted data series

    Raises
    ------
    _Skip
        if this data structure doesn't overlap with the requested
        ``[start, end)`` interval.
    """
    datastart = epoch + frdata.GetTimeOffset()
    try:
        trange = frdata.GetTRange()
    except AttributeError:  # not proc channel
        trange = 0.

    # check overlap with user-requested span
    if (end and datastart >= end) or (trange and datastart + trange < start):
        raise _Skip()

    out = None
    for j in range(frdata.data.size()):
        # we use range(frdata.data.size()) to avoid segfault
        # related to iterating directly over frdata.data
        try:
            new = read_frvect(frdata.data[j], datastart, start, end,
                              name=name, series_class=series_class)
        except _Skip:
            continue
        if out is None:
            out = new
        else:
            out.append(new)
    return out


def read_frvect(vect, epoch, start, end, series_class=TimeSeries, name=None):
    """Read an array from an `FrVect` structure

    Parameters
    ----------
    vect : `LDASTools.frameCPP.FrVect`
        the frame vector structur to read

    start : `float`
        the GPS start time of the request

    end : `float`
        the GPS end time of the request

    epoch : `float`
        the GPS start time of the containing `FrData` structure
    """
    # only read FrVect with matching name (or no name set)
    #    frame spec allows for arbitrary other FrVects
    #    to hold other information
    if vect.GetName() and name and vect.GetName() != name:
        raise _Skip()
    name = vect.GetName()

    # get array
    arr = vect.GetDataArray()
    nsamp = arr.size

    # and dimensions
    dim = vect.GetDim(0)
    dx = dim.dx
    x0 = dim.startX

    # start and end GPS times of this FrVect
    dimstart = epoch + x0
    dimend = dimstart + nsamp * dx

    # index of first required sample
    nxstart = int(max(0., float(start-dimstart)) / dx)

    # requested start time is after this frame, skip
    if nxstart >= nsamp:
        raise _Skip()

    # index of end sample
    if end:
        nxend = int(nsamp - ceil(max(0., float(dimend-end)) / dx))
    else:
        nxend = None

    if nxstart or nxend:
        arr = arr[nxstart:nxend]

    # -- cast as a series

    # get unit
    unit = vect.GetUnitY() or None

    # create array
    series = series_class(arr, t0=dimstart+nxstart*dx, dt=dx, name=name,
                          channel=name, unit=unit, copy=False)

    # add information to channel
    series.channel.sample_rate = series.sample_rate.value
    series.channel.unit = unit
    series.channel.dtype = series.dtype

    return series


# -- write --------------------------------------------------------------------

def write(tsdict, outfile, start=None, end=None, name='gwpy', run=0,
          compression=257, compression_level=6):
    """Write data to a GWF file using the frameCPP API
    """
    # set frame header metadata
    if not start:
        starts = {LIGOTimeGPS(tsdict[key].x0.value) for key in tsdict}
        if len(starts) != 1:
            raise RuntimeError("Cannot write multiple TimeSeries to a single "
                               "frame with different start times, "
                               "please write into different frames")
        start = list(starts)[0]
    if not end:
        ends = {tsdict[key].span[1] for key in tsdict}
        if len(ends) != 1:
            raise RuntimeError("Cannot write multiple TimeSeries to a single "
                               "frame with different end times, "
                               "please write into different frames")
        end = list(ends)[0]
    duration = end - start
    start = LIGOTimeGPS(start)
    ifos = {ts.channel.ifo for ts in tsdict.values() if
            ts.channel and ts.channel.ifo and
            hasattr(frameCPP, 'DETECTOR_LOCATION_{0}'.format(ts.channel.ifo))}

    # create frame
    frame = io_gwf.create_frame(time=start, duration=duration, name=name,
                                run=run, ifos=ifos)

    # append channels
    for i, key in enumerate(tsdict):
        try:
            # pylint: disable=protected-access
            ctype = tsdict[key].channel._ctype or 'proc'
        except AttributeError:
            ctype = 'proc'
        append_to_frame(frame, tsdict[key].crop(start, end),
                        type=ctype, channelid=i)

    # write frame to file
    io_gwf.write_frames(outfile, [frame], compression=compression,
                        compression_level=compression_level)


def append_to_frame(frame, timeseries, type='proc', channelid=0):
    # pylint: disable=redefined-builtin
    """Append data from a `TimeSeries` to a `~frameCPP.FrameH`

    Parameters
    ----------
    frame : `~frameCPP.FrameH`
        frame object to append to

    timeseries : `TimeSeries`
        the timeseries to append

    type : `str`
        the type of the channel, one of 'adc', 'proc', 'sim'

    channelid : `int`, optional
        the ID of the channel within the group (only used for ADC channels)
    """
    if timeseries.channel:
        channel = str(timeseries.channel)
    else:
        channel = str(timeseries.name)

    offset = timeseries.t0.value - float(LIGOTimeGPS(*frame.GetGTime()))

    # create the data container
    if type.lower() == 'adc':
        frdata = frameCPP.FrAdcData(
            channel,
            0,  # channel group
            channelid,  # channel number in group
            16,  # number of bits in ADC
            timeseries.sample_rate.value,  # sample rate
        )
        append = frame.AppendFrAdcData
    elif type.lower() == 'proc':
        frdata = frameCPP.FrProcData(
            channel,  # channel name
            str(timeseries.name),  # comment
            frameCPP.FrProcData.TIME_SERIES,  # ID as time-series
            frameCPP.FrProcData.UNKNOWN_SUB_TYPE,  # empty sub-type (fseries)
            offset,  # offset of first sample relative to frame start
            abs(timeseries.span),  # duration of data
            0.,  # heterodyne frequency
            0.,  # phase of heterodyne
            0.,  # frequency range
            0.,  # resolution bandwidth
        )
        append = frame.AppendFrProcData
    elif type.lower() == 'sim':
        frdata = frameCPP.FrSimData(
            str(timeseries.channel),  # channel name
            str(timeseries.name),  # comment
            timeseries.sample_rate.value,  # sample rate
            offset,  # time offset of first sample
            0.,  # heterodyne frequency
            0.,  # phase of heterodyne
        )
        append = frame.AppendFrSimData
    else:
        raise RuntimeError("Invalid channel type {!r}, please select one of "
                           "'adc, 'proc', or 'sim'".format(type))
    # append an FrVect
    frdata.AppendData(create_frvect(timeseries))
    append(frdata)


def create_frvect(timeseries):
    """Create a `~frameCPP.FrVect` from a `TimeSeries`

    This method is primarily designed to make writing data to GWF files a
    bit easier.

    Parameters
    ----------
    timeseries : `TimeSeries`
        the input `TimeSeries`

    Returns
    -------
    frvect : `~frameCPP.FrVect`
        the output `FrVect`
    """
    # create timing dimension
    dims = frameCPP.Dimension(
        timeseries.size, timeseries.dx.value,
        str(timeseries.dx.unit), 0)
    # create FrVect
    vect = frameCPP.FrVect(
        timeseries.name or '', FRVECT_TYPE_FROM_NUMPY[timeseries.dtype.type],
        1, dims, str(timeseries.unit))
    # populate FrVect and return
    vect.GetDataArray()[:] = numpy.require(timeseries.value,
                                           requirements=['C'])
    return vect
