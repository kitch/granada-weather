const forecastElement = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const forecastValue = (value, suffix = '') => value == null ? '—' : `${value}${suffix}`;

function forecastDetail(label, value) {
  const item = forecastElement('div');
  item.append(forecastElement('span', '', label), forecastElement('b', '', value));
  return item;
}

function forecastCard(label, data, prefix) {
  const article = forecastElement('article', 'forecast-card');
  const header = forecastElement('header');
  header.append(
    forecastElement('h3', '', label),
    forecastElement('span', 'summary', data[`${prefix}_summary`] || 'Forecast')
  );

  const temperatures = forecastElement('div', 'forecast-temps');
  temperatures.append(
    forecastElement('strong', '', forecastValue(data[`${prefix}_high`], '°')),
    forecastElement('span', '', forecastValue(data[`${prefix}_low`], '°'))
  );

  const details = forecastElement('div', 'forecast-details');
  if (prefix === 'today') details.classList.add('today-details');
  details.append(
    forecastDetail('RAIN CHANCE', forecastValue(data[`${prefix}_rain_chance`], '%')),
    forecastDetail('RAIN TOTAL', forecastValue(data[`${prefix}_rain_in`], ' in'))
  );
  if (prefix !== 'today') {
    details.classList.add('tomorrow-details');
  }

  article.append(header, temperatures, details);
  return article;
}

function renderAstronomy(data) {
  document.querySelector('#sunrise').textContent = data.today_sunrise || '—';
  document.querySelector('#sunset').textContent = data.today_sunset || '—';
  document.querySelector('#moon-phase').textContent = data.today_moon_phase || '—';
}

function forecastMessage(message) {
  return forecastElement('div', 'forecast-empty', message);
}

function renderAttribution(data) {
  const container = document.querySelector('#forecast-attribution');
  const attribution = data.attribution || {};
  container.replaceChildren();
  if (!attribution.url && !attribution.text) return;
  const link = forecastElement('a');
  link.href = attribution.url || '#';
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  if (attribution.logo) {
    const logo = forecastElement('img');
    logo.src = attribution.logo;
    logo.alt = attribution.text || `${data.provider_name || 'Forecast'} attribution`;
    link.append(logo);
  } else {
    link.textContent = attribution.text || data.provider_name || 'Forecast source';
  }
  container.append(link);
}

async function loadForecast(provider) {
  const cards = document.querySelector('#forecast-cards');
  try {
    const query = provider ? `?provider=${encodeURIComponent(provider)}` : '';
    const response = await fetch(`/api/forecast${query}`);
    if (!response.ok) throw new Error(`Forecast returned ${response.status}`);
    const data = await response.json();
    if (!data.updated_at) {
      cards.replaceChildren(forecastMessage('Forecast will appear after the next provider update.'));
      return;
    }
    cards.replaceChildren(forecastCard('Today', data, 'today'), forecastCard('Tomorrow', data, 'tomorrow'));
    renderAstronomy(data);
    renderAttribution(data);
    if (data.provider && document.querySelector(`#forecast-provider option[value="${CSS.escape(data.provider)}"]`)) {
      document.querySelector('#forecast-provider').value = data.provider;
    }
  } catch (error) {
    cards.replaceChildren(forecastMessage('Forecast is temporarily unavailable.'));
  }
}

async function setupForecastProviders() {
  const select = document.querySelector('#forecast-provider');
  try {
    const response = await fetch('/api/forecast/providers');
    if (!response.ok) throw new Error(`Providers returned ${response.status}`);
    const data = await response.json();
    select.replaceChildren(...(data.providers || []).map(provider => {
      const option = forecastElement('option', '', provider.name);
      option.value = provider.id;
      return option;
    }));
    const saved = localStorage.getItem('granada-forecast-provider');
    const available = new Set((data.providers || []).map(provider => provider.id));
    select.value = available.has(saved) ? saved : (available.has(data.default) ? data.default : (data.providers?.[0]?.id || ''));
    select.addEventListener('change', () => {
      localStorage.setItem('granada-forecast-provider', select.value);
      loadForecast(select.value);
    });
    await loadForecast(select.value);
  } catch (error) {
    select.closest('.forecast-source').hidden = true;
    await loadForecast();
  }
}

setupForecastProviders();
setInterval(() => loadForecast(document.querySelector('#forecast-provider').value), 300000);
