# Copyright 2016 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from numpy import dtype, around, full, nan, concatenate

from six import iteritems

from zipline.pipeline.data.equity_pricing import USEquityPricing
from zipline.lib._float64window import AdjustedArrayWindow as Float64Window
from zipline.lib.adjustment import Float64Multiply
from zipline.utils.cache import CachedObject, Expired


class SlidingWindow(object):
    """
    Wrapper around an AdjustedArrayWindow which supports monotonically
    increasing (by datetime) requests for a sized window of data.

    Parameters
    ----------
    window : AdjustedArrayWindow
       Window of pricing data with prefetched values beyond the current
       simulation dt.
    cal_start : int
       Index in the overall calendar at which the window starts.
    """

    def __init__(self, window, size, cal_start, offset):
        self.window = window
        self.cal_start = cal_start
        self.current = around(next(window), 3)
        self.offset = offset
        self.most_recent_ix = self.cal_start + size

    def get(self, end_ix):
        """
        Returns
        -------
        out : A np.ndarray of the equity pricing up to end_ix after adjustments
              and rounding have been applied.
        """
        if self.most_recent_ix == end_ix:
            return self.current

        target = end_ix - self.cal_start - self.offset + 1
        self.current = around(self.window.seek(target), 3)

        self.most_recent_ix = end_ix
        return self.current


class USEquityHistoryLoader(object):
    """
    Loader for sliding history windows of adjusted US Equity Pricing data.

    Parameters
    ----------
    daily_reader : DailyBarReader
        Reader for daily bars.
    adjustment_reader : SQLiteAdjustmentReader
        Reader for adjustment data.
    """

    def __init__(self, env, daily_reader, adjustment_reader):
        self.env = env
        self._daily_reader = daily_reader
        self._calendar = daily_reader._calendar
        self._adjustments_reader = adjustment_reader
        self._daily_window_blocks = {}

        self._prefetch_length = 40

    def _get_adjustments_in_range(self, assets, days, field):
        """
        Get the Float64Multiply objects to pass to an AdjustedArrayWindow.

        For the use of AdjustedArrayWindow in the loader, which looks back
        from current simulation time back to a window of data the dictionary is
        structured with:
        - the key into the dictionary for adjustments is the location of the
        day from which the window is being viewed.
        - the start of all multiply objects is always 0 (in each window all
          adjustments are overlapping)
        - the end of the multiply object is the location before the calendar
          location of the adjustment action, making all days before the event
          adjusted.

        Parameters
        ----------
        assets : iterable of Asset
            The assets for which to get adjustments.

        days : iterable of datetime64-like
            The days for which adjustment data is needed.
        field : str
            OHLCV field for which to get the adjustments.

        Returns
        -------
        out : The adjustments as a dict of loc -> Float64Multiply
        """
        sids = {int(asset): i for i, asset in enumerate(assets)}
        start = days[0]
        end = days[-1]
        adjs = {}
        for sid, i in iteritems(sids):
            if field != 'volume':
                mergers = self._adjustments_reader.get_adjustments_for_sid(
                    'mergers', sid)
                for m in mergers:
                    dt = m[0]
                    if start < dt <= end:
                        end_loc = days.get_loc(dt)
                        mult = Float64Multiply(0,
                                               end_loc - 1,
                                               i,
                                               i,
                                               m[1])
                        try:
                            adjs[end_loc].append(mult)
                        except KeyError:
                            adjs[end_loc] = [mult]
                divs = self._adjustments_reader.get_adjustments_for_sid(
                    'dividends', sid)
                for d in divs:
                    dt = d[0]
                    if start < dt <= end:
                        end_loc = days.get_loc(dt)
                        mult = Float64Multiply(0,
                                               end_loc - 1,
                                               i,
                                               i,
                                               d[1])
                        try:
                            adjs[end_loc].append(mult)
                        except KeyError:
                            adjs[end_loc] = [mult]
            splits = self._adjustments_reader.get_adjustments_for_sid(
                'splits', sid)
            for s in splits:
                dt = s[0]
                if field == 'volume':
                    ratio = 1.0 / s[1]
                else:
                    ratio = s[1]
                if start < dt <= end:
                    end_loc = days.get_loc(dt)
                    mult = Float64Multiply(0,
                                           end_loc - 1,
                                           i,
                                           i,
                                           ratio)
                    try:
                        adjs[end_loc].append(mult)
                    except KeyError:
                        adjs[end_loc] = [mult]
        return adjs

    def _ensure_sliding_window(
            self, assets, start, end, size, field):
        assets_key = frozenset(assets)
        try:
            block_cache = self._daily_window_blocks[(assets_key, field, size)]
            try:
                return block_cache.unwrap(end)
            except Expired:
                pass
        except KeyError:
            pass

        # Handle case where data is request before the start of data available
        # in the daily_reader. In that case, prepend nans until the start
        # of data.
        pre_array = None
        if start < self._daily_reader._calendar[0]:
            start_ix = 0
            td = self.env.trading_days
            offset = td.get_loc(start) - td.get_loc(
                self._daily_reader._calendar[0])
            if end < self._daily_reader._calendar[0]:
                fill_size = size
                end_ix = 0
            else:
                pre_slice = self._calendar.slice_indexer(start, end)
                fill_size = pre_slice.stop - pre_slice.start
                end_ix = self._calendar.get_loc(end)
            if field != 'volume':
                pre_array = full((fill_size, 1), nan)
            else:
                pre_array = full((fill_size, 1), 0)
        else:
            offset = 0
            start_ix = self._calendar.get_loc(start)
            end_ix = self._calendar.get_loc(end)

        col = getattr(USEquityPricing, field)
        cal = self._calendar
        prefetch_end_ix = min(end_ix + self._prefetch_length, len(cal) - 1)
        prefetch_end = cal[prefetch_end_ix]

        days = cal[start_ix:prefetch_end_ix + 1]
        array = self._daily_reader.load_raw_arrays(
            [col], days[0], prefetch_end, assets)[0]
        if self._adjustments_reader:
            adjs = self._get_adjustments_in_range(assets, days, col)
        else:
            adjs = {}
        if field == 'volume':
            array = array.astype('float64')
        dtype_ = dtype('float64')

        if pre_array is not None:
            array = concatenate([pre_array, array])

        window = Float64Window(
            array,
            dtype_,
            adjs,
            0,
            size
        )
        block = SlidingWindow(window, size, start_ix, offset)
        self._daily_window_blocks[(assets_key, field, size)] = CachedObject(
            block, prefetch_end)
        return block

    def history(self, assets, dts, field):
        """
        A window of pricing data with adjustments applied assuming that the
        end of the window is the day before the current simulation time.

        Parameters
        ----------
        assets : iterable of Assets
            The assets in the window.
        dts : iterable of datetime64-like
            The datetimes for which to fetch data.
            Makes an assumption that all dts are present and contiguous,
            in the calendar.
        field : str
            The OHLCV field for which to retrieve data.


        Returns
        -------
        out : np.ndarray with shape(len(days between start, end), len(assets))
        """
        start = dts[0]
        end = dts[-1]
        size = len(dts)
        block = self._ensure_sliding_window(assets, start, end, size, field)
        if end > self._calendar[0]:
            end_ix = self._calendar.get_loc(end)
        else:
            end_ix = size
        return block.get(end_ix)


class USEquityMinuteHistoryLoader(object):

    def __init__(self, env, minute_reader, adjustment_reader):
        self.env = env
        self._minute_reader = minute_reader
#        self._market_minutes = self.env.minutes_for_days_in_range(
#            minute_reader.first_trading_day, self.env.last_trading_day)
        self._adjustments_reader = adjustment_reader
        self._minute_window_blocks = {}

        self._prefetch_length = 5 * 390

    def _get_adjustments_in_range(self, assets, days, field):
        """
        Get the Float64Multiply objects to pass to an AdjustedArrayWindow.

        For the use of AdjustedArrayWindow in the loader, which looks back
        from current simulation time back to a window of data the dictionary is
        structured with:
        - the key into the dictionary for adjustments is the location of the
        day from which the window is being viewed.
        - the start of all multiply objects is always 0 (in each window all
          adjustments are overlapping)
        - the end of the multiply object is the location before the calendar
          location of the adjustment action, making all days before the event
          adjusted.

        Parameters
        ----------
        assets : iterable of Asset
            The assets for which to get adjustments.

        days : iterable of datetime64-like
            The days for which adjustment data is needed.
        field : str
            OHLCV field for which to get the adjustments.

        Returns
        -------
        out : The adjustments as a dict of loc -> Float64Multiply
        """
        sids = {int(asset): i for i, asset in enumerate(assets)}
        start = days[0]
        end = days[-1]
        adjs = {}
        for sid, i in iteritems(sids):
            if field != 'volume':
                mergers = self._adjustments_reader.get_adjustments_for_sid(
                    'mergers', sid)
                for m in mergers:
                    dt = m[0]
                    if start < dt <= end:
                        end_loc = days.get_loc(dt)
                        mult = Float64Multiply(0,
                                               end_loc - 1,
                                               i,
                                               i,
                                               m[1])
                        try:
                            adjs[end_loc].append(mult)
                        except KeyError:
                            adjs[end_loc] = [mult]
                divs = self._adjustments_reader.get_adjustments_for_sid(
                    'dividends', sid)
                for d in divs:
                    dt = d[0]
                    if start < dt <= end:
                        end_loc = days.get_loc(dt)
                        mult = Float64Multiply(0,
                                               end_loc - 1,
                                               i,
                                               i,
                                               d[1])
                        try:
                            adjs[end_loc].append(mult)
                        except KeyError:
                            adjs[end_loc] = [mult]
            splits = self._adjustments_reader.get_adjustments_for_sid(
                'splits', sid)
            for s in splits:
                dt = s[0]
                if field == 'volume':
                    ratio = 1.0 / s[1]
                else:
                    ratio = s[1]
                if start < dt <= end:
                    end_loc = days.get_loc(dt)
                    mult = Float64Multiply(0,
                                           end_loc - 1,
                                           i,
                                           i,
                                           ratio)
                    try:
                        adjs[end_loc].append(mult)
                    except KeyError:
                        adjs[end_loc] = [mult]
        return adjs

    def _ensure_sliding_window(
            self, assets, start, end, size, field):
        assets_key = frozenset(assets)
        try:
            block_cache = self._daily_window_blocks[(assets_key, field, size)]
            try:
                return block_cache.unwrap(end)
            except Expired:
                pass
        except KeyError:
            pass

        # Handle case where data is request before the start of data available
        # in the daily_reader. In that case, prepend nans until the start
        # of data.
        pre_array = None
        if start < self._daily_reader._calendar[0]:
            start_ix = 0
            td = self.env.trading_days
            offset = td.get_loc(start) - td.get_loc(
                self._daily_reader._calendar[0])
            if end < self._daily_reader._calendar[0]:
                fill_size = size
                end_ix = 0
            else:
                pre_slice = self._calendar.slice_indexer(start, end)
                fill_size = pre_slice.stop - pre_slice.start
                end_ix = self._calendar.get_loc(end)
            if field != 'volume':
                pre_array = full((fill_size, 1), nan)
            else:
                pre_array = full((fill_size, 1), 0)
        else:
            offset = 0
            start_ix = self._calendar.get_loc(start)
            end_ix = self._calendar.get_loc(end)

        col = getattr(USEquityPricing, field)
        cal = self._calendar
        prefetch_end_ix = min(end_ix + self._prefetch_length, len(cal) - 1)
        prefetch_end = cal[prefetch_end_ix]

        days = cal[start_ix:prefetch_end_ix + 1]
        array = self._daily_reader.load_raw_arrays(
            [col], days[0], prefetch_end, assets)[0]
        if self._adjustments_reader:
            adjs = self._get_adjustments_in_range(assets, days, col)
        else:
            adjs = {}
        if field == 'volume':
            array = array.astype('float64')
        dtype_ = dtype('float64')

        if pre_array is not None:
            array = concatenate([pre_array, array])

        window = Float64Window(
            array,
            dtype_,
            adjs,
            0,
            size
        )
        block = SlidingWindow(window, size, start_ix, offset)
        self._daily_window_blocks[(assets_key, field, size)] = CachedObject(
            block, prefetch_end)
        return block

    def history(self, assets, dts, field):
        """
        A window of pricing data with adjustments applied assuming that the
        end of the window is the day before the current simulation time.

        Parameters
        ----------
        assets : iterable of Assets
            The assets in the window.
        dts : iterable of datetime64-like
            The datetimes for which to fetch data.
            Makes an assumption that all dts are present and contiguous,
            in the calendar.
        field : str
            The OHLCV field for which to retrieve data.


        Returns
        -------
        out : np.ndarray with shape(len(days between start, end), len(assets))
        """
        start = dts[0]
        end = dts[-1]
        size = len(dts)
        block = self._ensure_sliding_window(assets, start, end, size, field)
        if end > self._calendar[0]:
            end_ix = self._calendar.get_loc(end)
        else:
            end_ix = size
        return block.get(end_ix)
