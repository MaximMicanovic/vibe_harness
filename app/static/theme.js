'use strict';

/* Automatic dark mode for the Vibe Harness dashboard.
 *
 * Sweden is the standard: the dashboard's reference clock is Europe/Stockholm
 * and "dark" is decided by the sun over the Gothenburg area (57.7089° N,
 * 11.9746° E). Dark mode is active from sunset until sunrise — no manual
 * toggle, no network, no external assets.
 *
 * But if the location is known, that location's information is used instead:
 * when the browser's Geolocation API reports a position, the sun is evaluated
 * at those coordinates and times are shown in the device's local time zone.
 * The last known position is cached in localStorage, so on later visits the
 * correct theme applies synchronously before first paint; it is refreshed
 * from the Geolocation API on every load. If geolocation is denied or
 * unavailable, Gothenburg stays the standard.
 *
 * Sunrise/sunset come from the NOAA sunrise equation (zenith 90.833° =
 * refraction + solar semi-diameter). This file is loaded synchronously in
 * <head> so the `dark` class is set on <html> before first paint (no flash
 * of the wrong theme), then re-checked every minute so the theme flips at
 * the actual sunrise/sunset transitions.
 *
 * Every refresh dispatches a `vibe-theme` CustomEvent on `document` with
 * { dark, sunrise, sunset, now, place } so the UI can show why the theme is
 * what it is. The current state is always available as window.VibeTheme.state.
 */

(function () {
  var DEFAULT_PLACE = {
    name: 'Gothenburg, Sweden',
    lat: 57.7089,
    lon: 11.9746,
    timeZone: 'Europe/Stockholm',
  };
  var ZENITH_OFFICIAL = 90.833; // degrees
  var RAD = Math.PI / 180;
  var REFRESH_MS = 60 * 1000;
  var LOCATION_KEY = 'vibe-theme-location';
  var GEO_OPTIONS = { enableHighAccuracy: false, maximumAge: 10 * 60 * 1000, timeout: 5000 };

  var dateFmtCache = {};

  /* Local calendar date of `now` in the given IANA time zone. */
  function localDate(now, timeZone) {
    var fmt = dateFmtCache[timeZone];
    if (!fmt) {
      fmt = new Intl.DateTimeFormat('en-CA', {
        timeZone: timeZone,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      });
      dateFmtCache[timeZone] = fmt;
    }
    var parts = fmt.format(now).split('-'); // en-CA → YYYY-MM-DD
    return { year: +parts[0], month0: +parts[1] - 1, day: +parts[2] };
  }

  function dayOfYear(year, month0, day) {
    return Math.round((Date.UTC(year, month0, day) - Date.UTC(year, 0, 1)) / 86400000) + 1;
  }

  /* The device's own time zone — used when the location is known, since the
   * device is assumed to be where it was located. UTC if it can't be read. */
  function deviceTimeZone() {
    try {
      var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz) return tz;
    } catch (e) { /* fall through */ }
    return 'UTC';
  }

  /* NOAA sunrise equation: sunrise/sunset (as UTC Date instants) for the
   * given local calendar date at (latDeg, lonDeg). Either may be null on
   * polar night / midnight sun (reachable at high latitudes when the known
   * location is far from Gothenburg; the caller falls back to
   * prefers-color-scheme in that case). */
  function sunTimes(year, month0, day, latDeg, lonDeg) {
    var n = dayOfYear(year, month0, day);
    var lat = latDeg * RAD;
    var result = { sunrise: null, sunset: null };
    var events = [['sunrise', 6, -1], ['sunset', 18, 1]];
    for (var i = 0; i < events.length; i++) {
      var key = events[i][0];
      var hour = events[i][1];
      var sign = events[i][2];
      // Fractional year, evaluated at the approximate local hour of the event.
      var gamma = 2 * Math.PI / 365 * (n - 1 + (hour - 12) / 24);
      var eqtime = 229.18 * (0.000075 + 0.001868 * Math.cos(gamma)
        - 0.032077 * Math.sin(gamma) - 0.014615 * Math.cos(2 * gamma)
        - 0.040849 * Math.sin(2 * gamma));
      var decl = 0.006918 - 0.399912 * Math.cos(gamma) + 0.070257 * Math.sin(gamma)
        - 0.006758 * Math.cos(2 * gamma) + 0.000907 * Math.sin(2 * gamma)
        - 0.002697 * Math.cos(3 * gamma) + 0.00148 * Math.sin(3 * gamma);
      var cosHa = Math.cos(ZENITH_OFFICIAL * RAD) / (Math.cos(lat) * Math.cos(decl))
        - Math.tan(lat) * Math.tan(decl);
      if (cosHa > 1 || cosHa < -1) continue; // sun never rises / never sets today
      var ha = Math.acos(cosHa) / RAD; // hour angle in degrees; 1° = 4 minutes
      var minutes = 720 - 4 * lonDeg - eqtime + sign * 4 * ha;
      result[key] = new Date(Date.UTC(year, month0, day) + minutes * 60000);
    }
    return result;
  }

  /* Is it dark at `place` right now? Dark = before sunrise or at/after
   * sunset, local time at the place. */
  function current(now, place) {
    now = now || new Date();
    place = place || api.place;
    var d = localDate(now, place.timeZone);
    var times = sunTimes(d.year, d.month0, d.day, place.lat, place.lon);
    var dark;
    if (times.sunrise && times.sunset) {
      dark = now < times.sunrise || now >= times.sunset;
    } else if (typeof window !== 'undefined' && window.matchMedia) {
      // Polar day/night fallback (reachable at high latitudes).
      dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    } else {
      dark = false;
    }
    return { dark: dark, sunrise: times.sunrise, sunset: times.sunset, now: now, place: place };
  }

  /* Switch to a known location (degrees) and re-apply the theme. */
  function useLocation(lat, lon) {
    if (typeof lat !== 'number' || typeof lon !== 'number'
        || !isFinite(lat) || !isFinite(lon)) {
      return api.state;
    }
    var place = { name: 'Your location', lat: lat, lon: lon, timeZone: deviceTimeZone() };
    api.place = place;
    cachePlace(place);
    return apply();
  }

  function loadCachedPlace() {
    if (typeof window === 'undefined') return null;
    try {
      var raw = window.localStorage.getItem(LOCATION_KEY);
      if (!raw) return null;
      var p = JSON.parse(raw);
      if (typeof p.lat !== 'number' || typeof p.lon !== 'number'
          || !isFinite(p.lat) || !isFinite(p.lon)) return null;
      return {
        name: (typeof p.name === 'string' && p.name) || 'Your location',
        lat: p.lat,
        lon: p.lon,
        timeZone: (typeof p.timeZone === 'string' && p.timeZone) || deviceTimeZone(),
      };
    } catch (e) {
      return null; // storage unavailable (private mode) or corrupt entry
    }
  }

  function cachePlace(place) {
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem(LOCATION_KEY, JSON.stringify(place));
    } catch (e) { /* storage unavailable (private mode) */ }
  }

  /* Ask the browser where it is; on success the theme follows that location
   * instead of Gothenburg. Denied/unavailable → keep the current place. */
  function detectLocation() {
    if (typeof navigator === 'undefined' || !navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        if (pos && pos.coords) useLocation(pos.coords.latitude, pos.coords.longitude);
      },
      function () { /* denied or timed out: keep the standard */ },
      GEO_OPTIONS
    );
  }

  var api = {
    place: DEFAULT_PLACE,
    state: { dark: false, sunrise: null, sunset: null, now: null, place: DEFAULT_PLACE },
    current: current,
    sunTimes: sunTimes,
    useLocation: useLocation,
    refresh: apply,
    start: function (intervalMs) {
      var cached = loadCachedPlace();
      if (cached) api.place = cached;
      apply();
      detectLocation();
      if (typeof window !== 'undefined' && window.setInterval) {
        window.setInterval(apply, intervalMs || REFRESH_MS);
      }
    },
  };

  function apply() {
    var info = current();
    api.state = info;
    if (typeof document !== 'undefined') {
      document.documentElement.classList.toggle('dark', info.dark);
      document.dispatchEvent(new CustomEvent('vibe-theme', { detail: info }));
    }
    return info;
  }

  if (typeof window !== 'undefined') {
    window.VibeTheme = api;
    api.start();
  }
  // Allow the sun math to be unit-checked outside the browser (node).
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
