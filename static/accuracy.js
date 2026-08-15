const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const value = (number, suffix = '', digits = 1) =>
  number == null ? '—' : `${Number(number).toFixed(digits).replace(/\.0$/, '')}${suffix}`;

const dateLabel = date => new Intl.DateTimeFormat(undefined, {
  weekday: 'long', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC'
}).format(new Date(`${date}T12:00:00Z`));

const timeLabel = timestamp => timestamp ? new Intl.DateTimeFormat(undefined, {
  month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
}).format(new Date(timestamp)) : '—';

function metric(label, number, suffix, digits = 1) {
  const wrapper = element('div', 'score-metric');
  wrapper.append(element('span', '', label), element('strong', '', value(number, suffix, digits)));
  return wrapper;
}

function providerCard(provider) {
  const card = element('article', 'provider-card');
  const header = element('header');
  header.append(element('h3', '', provider.name), element('span', '', `${provider.days} scored day${provider.days === 1 ? '' : 's'}`));
  const scores = element('div', 'score-grid');
  scores.append(
    metric('HIGH ERROR', provider.high_mae_f, '°F'),
    metric('LOW ERROR', provider.low_mae_f, '°F'),
    metric('RAIN ERROR', provider.rain_mae_in, ' in', 3),
    metric('WET-DAY ACCURACY', provider.wet_accuracy_pct, '%')
  );
  card.append(header, scores);
  return card;
}

function actual(label, number, suffix, digits = 1) {
  const wrapper = element('div');
  wrapper.append(element('span', '', label), element('strong', '', value(number, suffix, digits)));
  return wrapper;
}

function dailyTable(day) {
  const table = element('table', 'daily-table');
  const head = element('thead');
  const headRow = element('tr');
  ['PROVIDER', 'HIGH', 'ERROR', 'LOW', 'ERROR', 'RAIN CHANCE', 'RAIN'].forEach(label => headRow.append(element('th', '', label)));
  head.append(headRow);
  const body = element('tbody');
  day.providers.forEach(provider => {
    const row = element('tr');
    [provider.provider.replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase()), value(provider.high_f, '°'), value(provider.high_error_f, '°'), value(provider.low_f, '°'), value(provider.low_error_f, '°'), value(provider.rain_chance_pct, '%', 0), value(provider.rain_in, ' in', 3)].forEach(text => row.append(element('td', '', text)));
    body.append(row);
  });
  table.append(head, body);
  return table;
}

function dayCard(day, open) {
  const details = element('details', 'day-card');
  details.open = open;
  const summary = element('summary');
  const actuals = element('div', 'actuals');
  actuals.append(actual('ACTUAL HIGH', day.actual.high_f, '°'), actual('ACTUAL LOW', day.actual.low_f, '°'), actual('ACTUAL RAIN', day.actual.rain_in, ' in', 3));
  summary.append(element('h3', '', dateLabel(day.date)), actuals);
  details.append(summary, dailyTable(day));
  return details;
}

async function loadAccuracy() {
  const providerScores = document.querySelector('#provider-scores');
  const dailyResults = document.querySelector('#daily-results');
  try {
    const response = await fetch('/api/forecast/accuracy');
    if (!response.ok) throw new Error(`Accuracy returned ${response.status}`);
    const data = await response.json();
    document.querySelector('#scored-days').textContent = data.scored_days ?? 0;
    document.querySelector('#observed-through').textContent = data.observed_through ? dateLabel(data.observed_through) : 'Waiting for a completed day';
    document.querySelector('#accuracy-updated').textContent = timeLabel(data.updated_at);
    document.querySelector('#accuracy-method').textContent = data.method || 'Scores use completed local station days.';
    const scored = (data.providers || []).filter(provider => provider.days > 0);
    providerScores.replaceChildren(...(scored.length ? scored.map(providerCard) : [element('div', 'accuracy-empty', 'The archive is ready. The first fair day-ahead score will appear after a full forecasted day has completed.') ]));
    dailyResults.replaceChildren(...(data.days?.length ? data.days.map((day, index) => dayCard(day, index === 0)) : [element('div', 'accuracy-empty', 'No completed day-ahead comparisons yet.') ]));
  } catch (error) {
    providerScores.replaceChildren(element('div', 'accuracy-empty', 'Forecast accuracy is temporarily unavailable.'));
  }
}

loadAccuracy();

