(() => {
  const lookup = document.querySelector('[data-lake-lookup]');
  if (!lookup) return;

  const searchButton = lookup.querySelector('[data-lake-search]');
  const manualButton = lookup.querySelector('[data-lake-manual]');
  const status = lookup.querySelector('[data-lake-status]');
  const results = lookup.querySelector('[data-lake-results]');
  const lakeName = document.getElementById('lake');
  const country = document.getElementById('country');
  const optionalFields = [
    'depth', 'area', 'latitude', 'longitude', 'elevation_m', 'mean_depth_m',
    'volume_km3', 'shoreline_length_km', 'catchment_area_km2',
    'residence_time_days', 'waterbody_type', 'administrative_region',
    'external_source', 'external_id', 'source_url', 'metadata_retrieved_at'
  ];

  function field(name) {
    return document.getElementById(name);
  }

  function setField(name, value) {
    const input = field(name);
    if (!input || value === null || value === undefined) return;
    if (input.tagName === 'SELECT' && !Array.from(input.options).some(option => option.value === String(value))) {
      input.add(new Option(String(value), String(value)));
    }
    input.value = String(value);
  }

  function useManualEntry() {
    optionalFields.forEach(name => {
      const input = field(name);
      if (input) input.value = '';
    });
    results.hidden = true;
    results.replaceChildren();
    status.textContent = 'Manual entry enabled. Only lake name and country are required.';
    lakeName.focus();
  }

  function applyCandidate(candidate) {
    setField('lake', candidate.lake);
    setField('country', candidate.country);
    optionalFields.forEach(name => setField(name, candidate[name]));
    const details = document.querySelector('[data-lake-details]');
    if (details) details.open = true;
    results.hidden = true;
    const warning = candidate.warnings?.length ? ` ${candidate.warnings.join(' ')}` : '';
    status.textContent = `Imported available metadata from Wikidata (${candidate.external_id}). Review it before saving.${warning}`;
  }

  function renderResults(candidates) {
    results.replaceChildren();
    if (!candidates.length) {
      results.hidden = true;
      status.textContent = 'No matching lakes were found. You can continue with manual entry.';
      return;
    }
    candidates.forEach(candidate => {
      const item = document.createElement('article');
      item.className = 'lake-lookup-result';
      const summary = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = candidate.lake;
      const description = document.createElement('span');
      description.textContent = candidate.description || [candidate.administrative_region, candidate.country].filter(Boolean).join(', ');
      summary.append(title, description);
      const selectButton = document.createElement('button');
      selectButton.type = 'button';
      selectButton.className = 'btn-secondary';
      selectButton.textContent = 'Use this result';
      selectButton.addEventListener('click', () => applyCandidate(candidate));
      item.append(summary, selectButton);
      results.append(item);
    });
    results.hidden = false;
    status.textContent = `${candidates.length} matching lake${candidates.length === 1 ? '' : 's'} found. Select the correct location.`;
  }

  async function search() {
    const name = lakeName.value.trim();
    if (name.length < 2) {
      status.textContent = 'Enter at least two characters of the lake name.';
      lakeName.focus();
      return;
    }
    const parameters = new URLSearchParams({ name });
    if (country.value) parameters.set('country', country.value);
    searchButton.disabled = true;
    searchButton.setAttribute('aria-busy', 'true');
    status.textContent = 'Searching Wikidata…';
    results.hidden = true;
    try {
      const response = await fetch(`${lookup.dataset.endpoint}?${parameters}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Lake lookup failed.');
      renderResults(payload.results || []);
    } catch (error) {
      results.replaceChildren();
      results.hidden = true;
      status.textContent = `${error.message} You can continue with manual entry.`;
    } finally {
      searchButton.disabled = false;
      searchButton.removeAttribute('aria-busy');
    }
  }

  searchButton.addEventListener('click', search);
  manualButton.addEventListener('click', useManualEntry);
})();
