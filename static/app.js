const $ = selector => document.querySelector(selector);
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const metrics = {
  outdoor_temp_f: {label: 'Outdoor temperature', short: 'Outdoor temp', unit: '°F', group: 'Air', digits: 1, rangeSeries: true},
  outdoor_humidity_pct: {label: 'Outdoor humidity', short: 'Outdoor humidity', unit: '%', group: 'Air', digits: 0, rangeSeries: true},
  feels_like_f: {label: 'Feels like', short: 'Feels like', unit: '°F', group: 'Air', digits: 1, rangeSeries: true},
  dew_point_f: {label: 'Dew point', short: 'Dew point', unit: '°F', group: 'Air', digits: 1, legacy: 'dewptf', rangeSeries: true},
  indoor_temp_f: {label: 'Indoor temperature', short: 'Indoor temp', unit: '°F', group: 'Indoor', digits: 1, rangeSeries: true},
  indoor_humidity_pct: {label: 'Indoor humidity', short: 'Indoor humidity', unit: '%', group: 'Indoor', digits: 0, rangeSeries: true},
  wind_speed_mph: {label: 'Wind speed', short: 'Wind', unit: 'mph', group: 'Wind', digits: 1, zeroBased: true, minUpper: 1},
  wind_gust_mph: {label: 'Wind gust', short: 'Gust', unit: 'mph', group: 'Wind', digits: 1, zeroBased: true, minUpper: 1},
  solar_radiation_wm2: {label: 'Solar radiation', short: 'Solar', unit: 'W/m²', group: 'Atmosphere', digits: 0, zeroBased: true, minUpper: 25},
  uv_index: {label: 'UV index', short: 'UV', unit: '', group: 'Atmosphere', digits: 0, zeroBased: true, minUpper: 1},
  pressure_relative_inhg: {label: 'Pressure', short: 'Pressure', unit: 'hPa', group: 'Atmosphere', digits: 0, rangeSeries: true},
  rain_rate_in_hr: {label: 'Rain rate', short: 'Rain rate', unit: 'in/hr', group: 'Rain', digits: 2, legacy: 'rain_hour_in', zeroBased: true, minUpper: 0.1},
  rainfall_in: {label: 'Rainfall', short: 'Rainfall', unit: 'in', group: 'Rain', digits: 3, bars: true},
  wind_direction_deg: {label: 'Wind direction', short: 'Direction', unit: '°', group: 'Wind', digits: 0, detail: true, history: false},
  rain_hour_in: {label: 'Rain this hour', short: 'Hourly rain', unit: 'in', group: 'Rain', digits: 3, legacy: 'rainin', history: false},
  rain_daily_in: {label: 'Rain today', short: 'Daily rain', unit: 'in', group: 'Rain', digits: 3, history: false},
  rain_weekly_in: {label: 'Rain this week', short: 'Weekly rain', unit: 'in', group: 'Rain', digits: 3, history: false},
  rain_monthly_in: {label: 'Rain this month', short: 'Monthly rain', unit: 'in', group: 'Rain', digits: 3, history: false},
  rain_yearly_in: {label: 'Rain this year', short: 'Yearly rain', unit: 'in', group: 'Rain', digits: 3, history: false}
};

let hours = 24;
let history = [];
let current = {};
let pressureTrendHistory = [];
let selected = 'outdoor_temp_f';
let historyEnd = null;
let lastHistoryLoad = 0;
let historyRequest = 0;
let chartModel = null;
let scrubTimestamp = null;
let scrubbing = false;
const HISTORY_REFRESH_MS = 10 * 60 * 1000;
const LONG_RANGE_HOURS = 24 * 30;

function raw(row, key) {
  const metric = metrics[key];
  if (key === 'feels_like_f') return feelsLike(row).value;
  const value = row?.[key] ?? (metric.legacy ? row?.[metric.legacy] : undefined);
  if (key === 'pressure_relative_inhg' && Number.isFinite(Number(value))) return Number(value) * 33.8638866667;
  return value;
}

function heatIndexF(temperature, humidity) {
  const temp = Number(temperature);
  const relativeHumidity = Number(humidity);
  if (!Number.isFinite(temp) || !Number.isFinite(relativeHumidity) || temp < 80 || relativeHumidity < 40) return null;

  const simple = 0.5 * (temp + 61 + (temp - 68) * 1.2 + relativeHumidity * 0.094);
  if ((temp + simple) / 2 < 80) return null;

  let result = -42.379
    + 2.04901523 * temp
    + 10.14333127 * relativeHumidity
    - 0.22475541 * temp * relativeHumidity
    - 0.00683783 * temp * temp
    - 0.05481717 * relativeHumidity * relativeHumidity
    + 0.00122874 * temp * temp * relativeHumidity
    + 0.00085282 * temp * relativeHumidity * relativeHumidity
    - 0.00000199 * temp * temp * relativeHumidity * relativeHumidity;

  if (relativeHumidity < 13 && temp >= 80 && temp <= 112) {
    result -= ((13 - relativeHumidity) / 4) * Math.sqrt((17 - Math.abs(temp - 95)) / 17);
  } else if (relativeHumidity > 85 && temp >= 80 && temp <= 87) {
    result += ((relativeHumidity - 85) / 10) * ((87 - temp) / 5);
  }
  return result;
}

function calculatedWindChillF(temperature, windSpeed) {
  const temp = Number(temperature);
  const wind = Number(windSpeed);
  if (!Number.isFinite(temp) || !Number.isFinite(wind) || temp > 50 || wind <= 3) return null;
  return 35.74 + 0.6215 * temp - 35.75 * wind ** 0.16 + 0.4275 * temp * wind ** 0.16;
}

function feelsLike(row) {
  const temperature = Number(raw(row, 'outdoor_temp_f'));
  const heatIndex = heatIndexF(temperature, raw(row, 'outdoor_humidity_pct'));
  const windChill = calculatedWindChillF(temperature, raw(row, 'wind_speed_mph'));
  if (Number.isFinite(heatIndex) && heatIndex >= temperature + 0.5) return {label: 'HEAT INDEX', detail: 'Heat index', value: heatIndex};
  if (Number.isFinite(windChill) && windChill <= temperature - 0.5) return {label: 'WIND CHILL', detail: 'Wind chill', value: windChill};
  return {label: 'AIR TEMP', detail: 'Air temp', value: temperature};
}

function val(value, metric) {
  if (value == null || value === '' || !Number.isFinite(Number(value))) return '—';
  if (metric.binary) return Number(value) ? 'LOW' : 'OK';
  return Number(value).toFixed(metric.digits).replace(/\.0+$/, '');
}

function cardinal(direction) {
  const value = Number(direction);
  if (!Number.isFinite(value)) return '—';
  const index = ((Math.round(value / 45) % 8) + 8) % 8;
  return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][index];
}

function pressureTendency() {
  const currentPressure = Number(raw(current, 'pressure_relative_inhg'));
  const latestTime = new Date(current.observed_at).getTime();
  if (!Number.isFinite(currentPressure) || !Number.isFinite(latestTime)) return null;
  const targetTime = latestTime - 3 * 3600000;
  const candidates = pressureTrendHistory
    .map(row => ({time: new Date(row.observed_at).getTime(), value: Number(raw(row, 'pressure_relative_inhg'))}))
    .filter(point => Number.isFinite(point.time) && Number.isFinite(point.value) && point.time <= latestTime);
  if (!candidates.length) return null;
  const prior = candidates.reduce((best, point) => Math.abs(point.time - targetTime) < Math.abs(best.time - targetTime) ? point : best);
  if (latestTime - prior.time < 2 * 3600000) return null;
  const change = currentPressure - prior.value;
  if (change >= 0.7) return {symbol: '↑', label: 'Rising', change};
  if (change <= -0.7) return {symbol: '↓', label: 'Falling', change};
  return {symbol: '→', label: 'Steady', change};
}

function renderPressure() {
  const pressure = raw(current, 'pressure_relative_inhg');
  $('#pressure').textContent = val(pressure, metrics.pressure_relative_inhg);
  const tendency = pressureTendency();
  const trend = $('#pressure-trend');
  trend.textContent = tendency?.symbol || '—';
  const description = tendency
    ? `${tendency.label}: ${tendency.change >= 0 ? '+' : ''}${tendency.change.toFixed(1)} hPa over three hours`
    : 'Three-hour pressure trend unavailable';
  trend.title = description;
  trend.setAttribute('aria-label', description);
}

function available() {
  return Object.keys(metrics).filter(key => raw(current, key) != null || history.some(row => raw(row, key) != null));
}

function renderCurrent() {
  if (!current.received_at) return;
  const apparent = feelsLike(current);
  $('#temp').textContent = val(raw(current, 'outdoor_temp_f'), metrics.outdoor_temp_f);
  $('#apparent-temp').hidden = !Number.isFinite(apparent.value);
  $('#apparent-temp').textContent = Number.isFinite(apparent.value) ? `${apparent.label} ${val(apparent.value, metrics.outdoor_temp_f)}°F` : '';
  $('#dew-point').textContent = val(raw(current, 'dew_point_f'), metrics.dew_point_f);
  $('#rain-hour').textContent = val(raw(current, 'rain_hour_in'), metrics.rain_hour_in);
  $('#rain-today').textContent = val(raw(current, 'rain_daily_in'), metrics.rain_daily_in);
  const rainRate = Number(raw(current, 'rain_rate_in_hr'));
  const rainRateCurrent = $('#rain-rate-current');
  rainRateCurrent.hidden = !Number.isFinite(rainRate) || rainRate <= 0;
  rainRateCurrent.textContent = rainRateCurrent.hidden
    ? ''
    : `FALLING AT ${val(rainRate, metrics.rain_rate_in_hr)} IN/HR`;
  $('#wind-speed').textContent = val(raw(current, 'wind_speed_mph'), metrics.wind_speed_mph);
  $('#wind-direction').textContent = cardinal(raw(current, 'wind_direction_deg'));
  $('#uv-index').textContent = val(raw(current, 'uv_index'), metrics.uv_index);
  $('#solar-radiation').textContent = val(raw(current, 'solar_radiation_wm2'), metrics.solar_radiation_wm2);
  renderPressure();
  const age = (Date.now() - new Date(current.received_at)) / 1000;
  const live = age < 180;
  $('#status').className = `status ${live ? 'live' : 'waiting'}`;
  $('#status span').textContent = live ? 'Receiving live data' : 'Last reading is stale';
  $('#updated').textContent = `Updated ${new Date(current.observed_at).toLocaleString()}`;
}

function renderPicker() {
  const keys = available().filter(key => metrics[key].history !== false);
  if (!keys.includes(selected)) selected = keys[0] || 'outdoor_temp_f';
  const buttons = keys.map(key => {
    const button = element('button', key === selected ? 'active' : '', metrics[key].short);
    button.dataset.metric = key;
    button.addEventListener('click', () => {
      selected = key;
      renderPicker();
      draw();
    });
    return button;
  });
  $('#metric-picker').replaceChildren(...buttons);
}

function appendLiveReading(reading) {
  if (historyEnd || !history.length || !reading?.observed_at) return;
  const timestamp = new Date(reading.observed_at).getTime();
  if (!Number.isFinite(timestamp)) return;
  const cutoff = Date.now() - hours * 3600000;
  history = history.filter(row => {
    const rowTime = new Date(row.observed_at).getTime();
    return rowTime >= cutoff && row.observed_at !== reading.observed_at;
  });
  history.push(reading);
  $('#count').textContent = `${history.length} readings shown`;
  renderPicker();
  draw();
}

async function loadCurrent() {
  try {
    const response = await fetch('/api/current');
    if (!response.ok) throw new Error(`Current weather returned ${response.status}`);
    const latest = await response.json();
    current = latest;
    renderCurrent();
    renderPicker();
    appendLiveReading(latest);
  } catch (error) {
    $('#status').className = 'status waiting';
    $('#status span').textContent = 'Receiver unavailable';
  }
}

async function loadHistory() {
  const request = ++historyRequest;
  try {
    const historyQuery = new URLSearchParams({hours: String(hours)});
    if (historyEnd) historyQuery.set('end', historyEnd.toISOString());
    const response = await fetch(`/api/history?${historyQuery}`);
    if (!response.ok) throw new Error(`History returned ${response.status}`);
    const rows = await response.json();
    if (request !== historyRequest) return;
    history = rows;
    if (!historyEnd) pressureTrendHistory = rows;
    lastHistoryLoad = Date.now();
    $('#count').textContent = `${rows.length} readings shown`;
    renderPicker();
    renderPressure();
    renderHistoryWindow();
    draw();
  } catch (error) {
    if (request === historyRequest) $('#count').textContent = 'History unavailable';
  }
}

function renderHistoryWindow() {
  const end = historyEnd || new Date();
  const start = new Date(end.getTime() - hours * 3600000);
  const longWindow = hours >= 24 * 30;
  const format = date => date.toLocaleString([], longWindow
    ? {month: 'short', day: 'numeric', year: 'numeric'}
    : {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  $('#history-window-label').textContent = `${format(start)} – ${historyEnd ? format(end) : 'Now'}`;
  $('#history-next').disabled = !historyEnd;
  $('#history-now').hidden = !historyEnd;
}

function nearestPoint(points, timestamp) {
  let low = 0;
  let high = points.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (points[middle].x < timestamp) low = middle + 1;
    else high = middle;
  }
  if (low === 0) return points[0];
  const before = points[low - 1];
  const after = points[low];
  return timestamp - before.x <= after.x - timestamp ? before : after;
}

function formatScrubTime(timestamp) {
  const date = new Date(timestamp);
  const options = hours >= 24 * 30
    ? {month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit'}
    : {weekday: 'short', hour: 'numeric', minute: '2-digit'};
  return date.toLocaleString([], options);
}

function formatRainBucket(point) {
  const start = new Date(point.start);
  const end = new Date(point.start + point.duration);
  if (point.duration >= 24 * 3600000) {
    return start.toLocaleDateString([], {month: 'short', day: 'numeric', year: hours >= 8760 ? 'numeric' : undefined});
  }
  const day = start.toLocaleDateString([], {weekday: 'short'});
  const startTime = start.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  const endTime = end.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  return `${day} ${startTime}–${endTime}`;
}

function formatRangeBucket(point) {
  const start = new Date(point.start);
  if (hours < 8760) return start.toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'});
  const end = new Date(point.start + point.duration - 1);
  const startText = start.toLocaleDateString([], {month: 'short', day: 'numeric'});
  const endText = end.toLocaleDateString([], {month: 'short', day: 'numeric', year: 'numeric'});
  return `${startText}–${endText}`;
}

function rangeValue(key, value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return NaN;
  return key === 'pressure_relative_inhg' ? number * 33.8638866667 : number;
}

function bucketStart(timestamp, weekly = false) {
  const date = new Date(timestamp);
  date.setHours(0, 0, 0, 0);
  if (weekly) date.setDate(date.getDate() - ((date.getDay() + 6) % 7));
  return date.getTime();
}

function nextBucketStart(timestamp, weekly = false) {
  const date = new Date(timestamp);
  date.setDate(date.getDate() + (weekly ? 7 : 1));
  return date.getTime();
}

function aggregateRangePoints(points) {
  const dailyBuckets = new Map();
  points.forEach(point => {
    const start = bucketStart(point.x);
    if (!dailyBuckets.has(start)) dailyBuckets.set(start, []);
    dailyBuckets.get(start).push(point);
  });
  const daily = [...dailyBuckets.entries()].map(([start, bucket]) => {
    const duration = nextBucketStart(start) - start;
    return {
      start,
      duration,
      x: start + duration / 2,
      low: Math.min(...bucket.map(point => point.low)),
      high: Math.max(...bucket.map(point => point.high)),
      y: bucket.at(-1).y,
    };
  }).sort((a, b) => a.x - b.x);
  if (hours < 8760) return daily;

  const weeklyBuckets = new Map();
  daily.forEach(point => {
    const start = bucketStart(point.start, true);
    if (!weeklyBuckets.has(start)) weeklyBuckets.set(start, []);
    weeklyBuckets.get(start).push(point);
  });
  return [...weeklyBuckets.entries()].map(([start, bucket]) => {
    const duration = nextBucketStart(start, true) - start;
    return {
      start,
      duration,
      x: start + duration / 2,
      low: bucket.reduce((sum, point) => sum + point.low, 0) / bucket.length,
      high: bucket.reduce((sum, point) => sum + point.high, 0) / bucket.length,
      y: bucket.at(-1).y,
    };
  }).sort((a, b) => a.x - b.x);
}

function hideScrubber() {
  scrubTimestamp = null;
  $('#chart-readout').hidden = true;
  draw();
}

function drawScrubber(context, model) {
  const readout = $('#chart-readout');
  if (scrubTimestamp == null || !model.points.length) {
    readout.hidden = true;
    return;
  }
  const point = nearestPoint(model.points, scrubTimestamp);
  const x = (point.x - model.firstTime) / (model.lastTime - model.firstTime || 1) * model.width;
  const pointY = value => 12 + (model.upper - value) / (model.upper - model.lower) * (model.height - 18);
  const y = pointY(point.y);
  context.save();
  context.strokeStyle = 'rgba(165, 242, 205, .72)';
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(x, 0);
  context.lineTo(x, model.height);
  context.stroke();
  context.strokeStyle = '#a5f2cd';
  context.lineWidth = 2;
  if (model.metric.bars) {
    const barWidth = Math.max(2, point.duration / (model.lastTime - model.firstTime || 1) * model.width - 2);
    context.strokeRect(x - barWidth / 2, y, barWidth, model.height - y);
  } else if (model.rangeSeries) {
    [point.high, point.low].forEach(value => {
      context.fillStyle = '#08120f';
      context.beginPath();
      context.arc(x, pointY(value), 5, 0, Math.PI * 2);
      context.fill();
      context.stroke();
    });
  } else {
    context.fillStyle = '#08120f';
    context.beginPath();
    context.arc(x, y, 5, 0, Math.PI * 2);
    context.fill();
    context.stroke();
  }
  context.restore();
  $('#chart-readout-time').textContent = model.metric.bars
    ? formatRainBucket(point)
    : model.rangeSeries ? formatRangeBucket(point) : formatScrubTime(point.x);
  const unit = model.metric.unit ? ` ${model.metric.unit}` : '';
  $('#chart-readout-value').textContent = model.rangeSeries
    ? `HIGH ${val(point.high, model.metric)}${unit} · LOW ${val(point.low, model.metric)}${unit}`
    : val(point.y, model.metric) + unit + (point.note ? ` · ${point.note}` : '');
  readout.style.left = `${x}px`;
  readout.dataset.edge = x < 90 ? 'left' : x > model.width - 90 ? 'right' : 'center';
  readout.hidden = false;
}

function scrubAtClientX(clientX) {
  if (!chartModel?.points.length) return;
  const rectangle = $('#chart').getBoundingClientRect();
  const x = Math.max(0, Math.min(rectangle.width, clientX - rectangle.left));
  const timestamp = chartModel.firstTime + x / (rectangle.width || 1) * (chartModel.lastTime - chartModel.firstTime);
  scrubTimestamp = nearestPoint(chartModel.points, timestamp).x;
  draw();
}

function draw() {
  const metric = metrics[selected];
  $('#chart-title').textContent = metric.label;
  const canvas = $('#chart');
  const context = canvas.getContext('2d');
  const rectangle = canvas.getBoundingClientRect();
  const scale = devicePixelRatio || 1;
  canvas.width = rectangle.width * scale;
  canvas.height = rectangle.height * scale;
  context.scale(scale, scale);
  const rangeSeries = Boolean(metric.rangeSeries && hours >= LONG_RANGE_HOURS);
  const sourcePoints = history
    .map(row => {
      const start = new Date(row.observed_at).getTime();
      const duration = Number(row.rain_bucket_seconds || 0) * 1000;
      const apparent = selected === 'feels_like_f' ? feelsLike(row) : null;
      const source = apparent ? apparent.value : raw(row, selected);
      const value = source == null || source === '' ? NaN : Number(source);
      const storedRange = row?._range?.[selected];
      const low = Array.isArray(storedRange) ? rangeValue(selected, storedRange[0]) : value;
      const high = Array.isArray(storedRange) ? rangeValue(selected, storedRange[1]) : value;
      return {x: start + (metric.bars ? duration / 2 : 0), start, duration, y: value, low, high, note: apparent?.detail};
    })
    .filter(point => Number.isFinite(point.x) && Number.isFinite(point.y) && Number.isFinite(point.low) && Number.isFinite(point.high) && (!metric.zeroBased || point.y >= 0))
    .sort((a, b) => a.x - b.x);
  const points = rangeSeries ? aggregateRangePoints(sourcePoints) : sourcePoints;
  $('#empty').style.display = points.length ? 'none' : 'grid';
  context.clearRect(0, 0, rectangle.width, rectangle.height);
  if (!points.length) {
    chartModel = null;
    $('#chart-readout').hidden = true;
    ['low', 'high', 'latest-value'].forEach(id => $(`#${id}`).textContent = '—');
    return;
  }
  const values = points.map(point => point.y);
  const rangeValues = history
    .map(row => row?._range?.[selected])
    .filter(range => Array.isArray(range) && range.length === 2 && range.every(value => Number.isFinite(Number(value))));
  const minimum = metric.bars
    ? 0
    : rangeSeries ? Math.min(...points.map(point => point.low)) : Math.min(...values, ...rangeValues.map(range => rangeValue(selected, range[0])));
  const maximum = rangeSeries
    ? Math.max(...points.map(point => point.high))
    : Math.max(...values, ...rangeValues.map(range => rangeValue(selected, range[1])));
  const windowMinimum = metric.bars ? 0 : Math.min(...sourcePoints.map(point => point.low));
  const windowMaximum = Math.max(...sourcePoints.map(point => point.high));
  const padding = Math.max(metric.binary ? 0.5 : metric.unit === 'in' ? 0.01 : 1, (maximum - minimum) * 0.15);
  const lower = metric.bars || metric.zeroBased ? 0 : metric.binary ? -0.15 : minimum - padding;
  const upper = metric.bars
    ? Math.max(0.01, maximum * 1.18)
    : metric.zeroBased
      ? Math.max(metric.minUpper || 1, maximum * 1.18)
      : metric.binary ? 1.15 : maximum + padding;
  const width = rectangle.width;
  const height = rectangle.height - 28;
  const requestedEnd = (historyEnd || new Date()).getTime();
  const requestedStart = requestedEnd - hours * 3600000;
  const firstTime = metric.bars ? points[0].start : requestedStart;
  const lastPoint = points.at(-1);
  const lastTime = metric.bars ? lastPoint.start + lastPoint.duration : requestedEnd;
  chartModel = {points, metric, width, height, firstTime, lastTime, lower, upper, rangeSeries};
  canvas.setAttribute('aria-label', rangeSeries
    ? `${metric.label} ${hours >= 8760 ? 'weekly average daily high and low' : 'daily high and low'} chart. Hover, drag, or use the arrow keys to inspect ranges.`
    : 'Historical weather chart. Hover, drag, or use the arrow keys to inspect readings.');
  context.strokeStyle = '#264137';
  context.fillStyle = '#84a095';
  context.font = '500 12px system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  for (let index = 0; index < 4; index++) {
    const y = 12 + index * (height - 18) / 3;
    const label = upper - index * (upper - lower) / 3;
    context.beginPath();
    context.moveTo(0, y);
    context.lineTo(width, y);
    context.stroke();
    context.fillText(metric.binary ? (label > 0.5 ? 'LOW' : 'OK') : val(label, metric) + metric.unit, 4, y - 4);
  }
  if (metric.bars) {
    context.fillStyle = 'rgba(81,211,154,.62)';
    points.forEach(point => {
      const x = (point.x - firstTime) / (lastTime - firstTime || 1) * width;
      const y = 12 + (upper - point.y) / (upper - lower) * (height - 18);
      const barWidth = Math.max(2, point.duration / (lastTime - firstTime || 1) * width - 2);
      context.fillRect(x - barWidth / 2, y, barWidth, height - y);
    });
  } else if (rangeSeries) {
    const high = points.map(point => ({
      x: (point.x - firstTime) / (lastTime - firstTime || 1) * width,
      y: 12 + (upper - point.high) / (upper - lower) * (height - 18),
    }));
    const low = points.map(point => ({
      x: (point.x - firstTime) / (lastTime - firstTime || 1) * width,
      y: 12 + (upper - point.low) / (upper - lower) * (height - 18),
    }));
    context.beginPath();
    high.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y));
    [...low].reverse().forEach(point => context.lineTo(point.x, point.y));
    context.closePath();
    context.fillStyle = 'rgba(81,211,154,.10)';
    context.fill();
    [
      {coordinates: high, color: '#a5f2cd', width: 2},
      {coordinates: low, color: '#668d7d', width: 1.5},
    ].forEach(line => {
      context.beginPath();
      line.coordinates.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y));
      context.strokeStyle = line.color;
      context.lineWidth = line.width;
      context.stroke();
    });
  } else {
    const intervals = points.slice(1).map((point, index) => point.x - points[index].x).filter(value => value > 0).sort((a, b) => a - b);
    const typicalInterval = intervals.length ? intervals[Math.floor(intervals.length / 2)] : Infinity;
    const gapThreshold = typicalInterval * 3.5;
    const segments = [];
    points.forEach(point => {
      const segment = segments.at(-1);
      if (!segment || point.x - segment.at(-1).x > gapThreshold) segments.push([point]);
      else segment.push(point);
    });
    segments.forEach(segment => {
      const coordinates = segment.map(point => ({
        x: (point.x - firstTime) / (lastTime - firstTime || 1) * width,
        y: 12 + (upper - point.y) / (upper - lower) * (height - 18),
      }));
      context.beginPath();
      coordinates.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y));
      context.strokeStyle = '#a5f2cd';
      context.lineWidth = 2;
      context.stroke();
      context.lineTo(coordinates.at(-1).x, height);
      context.lineTo(coordinates[0].x, height);
      context.closePath();
      context.fillStyle = 'rgba(81,211,154,.10)';
      context.fill();
    });
  }
  $('#low-label').textContent = metric.bars ? 'TOTAL' : 'LOW';
  $('#high-label').textContent = metric.bars ? 'WETTEST' : 'HIGH';
  $('#latest-label').textContent = metric.bars ? 'LATEST' : historyEnd ? 'LATEST' : 'CURRENT';
  $('#low').textContent = val(metric.bars ? values.reduce((sum, value) => sum + value, 0) : windowMinimum, metric) + (metric.unit ? ` ${metric.unit}` : '');
  $('#high').textContent = val(windowMaximum, metric) + (metric.unit ? ` ${metric.unit}` : '');
  const latestValue = metric.bars || historyEnd ? lastPoint.y : raw(current, selected);
  const formattedLatest = val(latestValue, metric);
  $('#latest-value').textContent = formattedLatest + (formattedLatest !== '—' && metric.unit ? ` ${metric.unit}` : '');
  if (rangeSeries) $('#count').textContent = `${points.length} ${hours >= 8760 ? 'weekly' : 'daily'} ranges shown`;
  drawScrubber(context, chartModel);
}

const chart = $('#chart');
chart.addEventListener('pointerdown', event => {
  scrubbing = true;
  chart.setPointerCapture?.(event.pointerId);
  scrubAtClientX(event.clientX);
});
chart.addEventListener('pointermove', event => {
  if (event.pointerType === 'mouse' || scrubbing) scrubAtClientX(event.clientX);
});
chart.addEventListener('pointerup', event => {
  scrubbing = false;
  chart.releasePointerCapture?.(event.pointerId);
});
chart.addEventListener('pointercancel', () => { scrubbing = false; });
chart.addEventListener('pointerleave', event => {
  if (event.pointerType === 'mouse' && !scrubbing) hideScrubber();
});
chart.addEventListener('keydown', event => {
  if (!chartModel?.points.length) return;
  const points = chartModel.points;
  let index = scrubTimestamp == null ? points.length - 1 : points.indexOf(nearestPoint(points, scrubTimestamp));
  if (event.key === 'ArrowLeft') index = Math.max(0, index - 1);
  else if (event.key === 'ArrowRight') index = Math.min(points.length - 1, index + 1);
  else if (event.key === 'Home') index = 0;
  else if (event.key === 'End') index = points.length - 1;
  else if (event.key === 'Escape') return hideScrubber();
  else return;
  event.preventDefault();
  scrubTimestamp = points[index].x;
  draw();
});

document.querySelectorAll('[data-hours]').forEach(button => button.addEventListener('click', () => {
  document.querySelector('.ranges .active').classList.remove('active');
  button.classList.add('active');
  hours = Number(button.dataset.hours);
  scrubTimestamp = null;
  loadHistory();
}));
$('#history-prev').addEventListener('click', () => {
  const end = historyEnd || new Date();
  historyEnd = new Date(end.getTime() - hours * 3600000);
  scrubTimestamp = null;
  loadHistory();
});
$('#history-next').addEventListener('click', () => {
  if (!historyEnd) return;
  const next = new Date(historyEnd.getTime() + hours * 3600000);
  historyEnd = next >= new Date() ? null : next;
  scrubTimestamp = null;
  loadHistory();
});
$('#history-now').addEventListener('click', () => {
  historyEnd = null;
  scrubTimestamp = null;
  loadHistory();
});
addEventListener('resize', draw);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  loadCurrent();
  if (!historyEnd && hours < 8760 && Date.now() - lastHistoryLoad >= HISTORY_REFRESH_MS) loadHistory();
});
loadCurrent();
loadHistory();
setInterval(() => {
  if (!document.hidden) loadCurrent();
}, 60000);
setInterval(() => {
  if (!document.hidden && !historyEnd && hours < 8760) loadHistory();
}, HISTORY_REFRESH_MS);
