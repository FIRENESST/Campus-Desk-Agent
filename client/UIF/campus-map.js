import { createCampusPhotoGallery } from './campus-photos.js';
import { createCampusMap3D } from './campus-map-3d.js';

const svgNS = 'http://www.w3.org/2000/svg';
const width = 540;
const height = 394;
const margin = 24;
const pinHitRadius = 18;
const pointZoom = 1.6;
const layers = ['campus_boundary', 'facility_area', 'building', 'walkway_candidate'];
const gridSize = 10000;
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const placeOf = coordinates => coordinates.map(value => Math.round(value * gridSize)).join('_');
const placeCoordinates = place => place.split('_').map(value => Number(value) / gridSize);

function svg(tag, attributes = {}) {
  const node = document.createElementNS(svgNS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

function coordinate(value) {
  return Array.isArray(value) && value.length === 2 && value.every(Number.isFinite);
}

function projector(positions) {
  if (!positions.length) throw new Error('校园底图缺少有效的坐标。');
  const longitudes = positions.map(point => point[0]);
  const latitudes = positions.map(point => point[1]);
  const west = Math.min(...longitudes), east = Math.max(...longitudes);
  const south = Math.min(...latitudes), north = Math.max(...latitudes);
  const midLat = (north + south) / 2;
  const midLon = (east + west) / 2;
  const longitudeScale = Math.cos(midLat * Math.PI / 180);
  const scale = Math.min((width - margin * 2) / ((east - west) * longitudeScale),
    (height - margin * 2) / (north - south));
  if (!Number.isFinite(scale) || scale <= 0) throw new Error('校园底图范围无效。');
  return {
    project: ([lon, lat]) => [width / 2 + (lon - midLon) * longitudeScale * scale, height / 2 - (lat - midLat) * scale],
    unproject: ([x, y]) => [midLon + (x - width / 2) / (longitudeScale * scale), midLat - (y - height / 2) / scale],
  };
}

function trace(points, project) {
  if (!Array.isArray(points) || points.length < 2 || !points.every(coordinate)) return '';
  return 'M' + points.map(point => project(point).map(value => value.toFixed(1)).join(',')).join(' L');
}

function inside([lon, lat], ring) {
  let hit = false;
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index++) {
    const [x, y] = ring[index];
    const [px, py] = ring[previous];
    if ((y > lat) !== (py > lat) && lon < (px - x) * (lat - y) / (py - y) + x) hit = !hit;
  }
  return hit;
}

function canPhotograph(place, ring) {
  const [lon, lat] = placeCoordinates(place);
  const shifts = [-.5 / gridSize, 0, .5 / gridSize];
  return shifts.some(dx => shifts.some(dy => inside([lon + dx, lat + dy], ring)));
}

function mapCamera(map, toolbar, element, choose, updateMarkers, signal) {
  const button = (text, label) => {
    const control = element('button', 'outline-button', text);
    control.type = 'button';
    control.setAttribute('aria-label', label);
    return control;
  };
  const out = button('−', '缩小地图');
  const plus = button('+', '放大地图');
  const reset = button('全图', '还原地图视角');
  const locate = button('定位', '定位所选点位');
  out.classList.add('map-zoom-button');
  plus.classList.add('map-zoom-button');
  locate.disabled = true;
  const level = element('output', 'map-zoom-level', '100%');
  level.setAttribute('aria-label', '缩放比例');
  toolbar.append(out, plus, reset, locate, level);
  let zoom = 1, x = 0, y = 0;
  let markerScale = 1, target;
  const pointers = new Map();
  let moved = false;

  function syncMarkers() {
    if (map.clientWidth) markerScale = width / zoom / map.clientWidth;
    updateMarkers(markerScale, { left: x, top: y, right: x + width / zoom, bottom: y + height / zoom });
  }

  function camera(nextZoom, left, top) {
    zoom = clamp(nextZoom, 1, 6);
    x = clamp(left, 0, width - width / zoom);
    y = clamp(top, 0, height - height / zoom);
    map.setAttribute('viewBox', `${x} ${y} ${width / zoom} ${height / zoom}`);
    syncMarkers();
    out.disabled = zoom <= 1;
    plus.disabled = zoom >= 6;
    reset.disabled = zoom === 1;
    level.value = Math.round(zoom * 100) + '%';
  }

  function locatePoint() {
    if (!target) return;
    const next = Math.max(zoom, pointZoom);
    camera(next, target[0] - width / next / 2, target[1] - height / next / 2);
  }

  function world(clientX, clientY) {
    return new DOMPoint(clientX, clientY).matrixTransform(map.getScreenCTM().inverse());
  }

  function magnify(factor, from, to = from) {
    const anchor = from ? world(from.x, from.y) : { x: x + width / zoom / 2, y: y + height / zoom / 2 };
    const destination = to ? world(to.x, to.y) : anchor;
    const next = clamp(zoom * factor, 1, 6);
    camera(next, anchor.x - (destination.x - x) * zoom / next, anchor.y - (destination.y - y) * zoom / next);
  }

  out.addEventListener('click', () => magnify(1 / 1.35));
  plus.addEventListener('click', () => magnify(1.35));
  reset.addEventListener('click', () => camera(1, 0, 0));
  locate.addEventListener('click', locatePoint);
  map.addEventListener('wheel', event => {
    if (!event.deltaY || (event.deltaY > 0 && zoom <= 1) || (event.deltaY < 0 && zoom >= 6)) return;
    event.preventDefault();
    magnify(event.deltaY < 0 ? 1.25 : 1 / 1.25, { x: event.clientX, y: event.clientY });
  }, { passive: false });
  map.addEventListener('keydown', event => {
    const step = 44 / zoom;
    const directions = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (Object.hasOwn(directions, event.key)) {
      const [dx, dy] = directions[event.key];
      camera(zoom, x + dx, y + dy);
    } else if (event.key === '+' || event.key === '=') magnify(1.35);
    else if (event.key === '-') magnify(1 / 1.35);
    else if (event.key === '0') camera(1, 0, 0);
    else return;
    event.preventDefault();
  });

  function gesture() {
    const [first, second] = pointers.values();
    if (!second) return { ...first, span: 0 };
    return { x: (first.x + second.x) / 2, y: (first.y + second.y) / 2,
      span: Math.hypot(first.x - second.x, first.y - second.y) };
  }

  map.addEventListener('pointerdown', event => {
    if (event.button !== 0 || pointers.size >= 2) return;
    if (!pointers.size) moved = false;
    else moved = true;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY });
    map.setPointerCapture(event.pointerId);
  });
  map.addEventListener('pointermove', event => {
    const pointer = pointers.get(event.pointerId);
    if (!pointer) return;
    const before = gesture();
    pointer.x = event.clientX;
    pointer.y = event.clientY;
    if (!moved && Math.hypot(pointer.x - pointer.startX, pointer.y - pointer.startY) < 4) return;
    moved = true;
    map.classList.add('is-panning');
    const after = gesture();
    magnify(before.span ? after.span / before.span : 1, before, after);
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    map.addEventListener(type, event => {
      const pointer = pointers.get(event.pointerId);
      if (!pointer) return;
      pointers.delete(event.pointerId);
      if (pointers.size) return;
      map.classList.remove('is-panning');
      if (type === 'pointerup' && !moved) choose(world(event.clientX, event.clientY), markerScale);
    });
  }
  const observer = new ResizeObserver(syncMarkers);
  observer.observe(map);
  signal.addEventListener('abort', () => observer.disconnect(), { once: true });
  camera(1, 0, 0);
  return {
    reset: () => camera(1, 0, 0),
    setTarget(point, reveal = false) {
      target = point;
      locate.disabled = false;
      if (reveal) locatePoint();
      else syncMarkers();
    },
  };
}

function mapLegend(element) {
  const legend = element('ul', 'map-legend');
  legend.setAttribute('aria-label', '地图图例');
  for (const [kind, label] of [['landmark', '校园地标'], ['photos', '有地点照片'], ['route', '候选步行路线']]) {
    const item = element('li', '', label);
    item.dataset.symbol = kind;
    legend.append(item);
  }
  return legend;
}

export function renderCampusMap({ base, survey, photoPlaces, photoError, request, signal }, element) {
  if (base.type !== 'FeatureCollection' || !Array.isArray(base.features)) {
    throw new Error('校园底图数据无法读取。');
  }
  const boundary = base.features.find(feature => feature.properties?.kind === 'campus_boundary');
  const ring = boundary?.geometry?.coordinates?.[0]?.filter(coordinate);
  if (!ring?.length) throw new Error('校园底图缺少校区范围。');
  const { project, unproject } = projector(ring);
  const map = svg('svg', { class: 'campus-map', viewBox: `0 0 ${width} ${height}`, tabindex: '0',
    role: 'region', 'aria-label': '校园底图，拖动或用方向键移动，滚轮、双指或加减键缩放' });
  const groups = new Map(layers.map(layer => [layer, svg('g', { class: `map-${layer}` })]));
  const pins = svg('g');
  const routeLine = svg('path', { class: 'map-planned-route', 'aria-hidden': 'true' });
  map.append(...groups.values(), routeLine, pins);

  const points = new Map();
  for (const { geometry, properties } of base.features) {
    const layer = groups.get(properties?.kind);
    const coords = geometry?.coordinates;
    if (layer && geometry?.type === 'Polygon' && Array.isArray(coords)) {
      const d = coords.map(part => trace(part, project)).filter(Boolean).map(path => path + ' Z').join(' ');
      if (d) layer.append(svg('path', { d, 'fill-rule': 'evenodd' }));
    } else if (layer && geometry?.type === 'LineString') {
      const d = trace(coords, project);
      if (d) layer.append(svg('path', { d }));
    } else if (geometry?.type === 'Point' && typeof properties?.name === 'string' && properties.name && coordinate(coords)) {
      points.set(properties.name, { name: properties.name, coordinates: coords });
    }
  }

  if (survey?.campus_id === base.metadata?.campus_id && Array.isArray(survey.points)) {
    for (const point of survey.points) {
      if (typeof point.name === 'string' && coordinate(point.coordinates)) points.set(point.name, point);
    }
    for (const place of survey.walking_places || []) {
      if (!points.has(place.name) && coordinate(place.coordinates)) points.set(place.name, place);
    }
  }

  const layout = element('div', 'campus-map-layout');
  const figure = element('figure', 'campus-map-figure');
  const toolbar = element('div', 'map-toolbar');
  const viewModes = element('div', 'map-modes');
  viewModes.setAttribute('role', 'group');
  viewModes.setAttribute('aria-label', '地图视图');
  const makeButton = (text, label = text) => {
    const control = element('button', 'outline-button', text);
    control.type = 'button';
    control.setAttribute('aria-label', label);
    return control;
  };
  const planar = makeButton('2D', '平面校园地图');
  const spatial = makeButton('3D', '立体校园地图');
  planar.setAttribute('aria-pressed', 'true');
  spatial.setAttribute('aria-pressed', 'false');
  viewModes.append(planar, spatial);
  const tools3d = element('div', 'map-toolbar');
  tools3d.hidden = true;
  const frame = element('div', 'map-frame');
  const compass = element('span', 'map-compass', '↑ 北');
  compass.setAttribute('aria-hidden', 'true');
  frame.append(map, compass);
  const map3d = element('div', 'map-spatial');
  map3d.hidden = true;
  const canvas = element('canvas', 'campus-map-canvas');
  canvas.tabIndex = 0;
  canvas.setAttribute('aria-label', '立体校园地图，拖动或用方向键旋转，滚轮、双指或加减键缩放');
  const pins3d = element('div', 'map-spatial-pins');
  const hint3d = element('span', 'map-spatial-hint', '拖动旋转 · 双指或滚轮缩放');
  map3d.append(canvas, pins3d, hint3d);
  frame.append(map3d);
  const tools = element('div', 'map-controls');
  tools.append(viewModes, toolbar, tools3d);
  const mapStatus = element('p', 'map-point-feedback');
  mapStatus.setAttribute('role', 'status');
  figure.append(frame, mapStatus, mapLegend(element));
  const selector = element('select');
  const markers = new Map();
  const pinLabel = svg('text', { class: 'map-pin-label', 'aria-hidden': 'true' });
  const details = element('p', 'map-point-details');
  details.setAttribute('role', 'status');
  const feedback = element('p', 'map-point-feedback');
  feedback.setAttribute('role', 'status');
  const gallery = createCampusPhotoGallery(element, request, signal, photoError);
  gallery.section.hidden = true;
  let custom;
  let selected;
  let scene;
  let routePath = [];

  function updateMarkers(scale, { left, top, right, bottom }) {
    for (const marker of markers.values()) marker.symbol.setAttribute('transform', `scale(${scale})`);
    if (!selected) return;
    const [x, y] = selected.projected;
    const visible = x >= left && x <= right && y >= top && y <= bottom;
    pinLabel.setAttribute('visibility', visible ? 'visible' : 'hidden');
    const onLeft = x <= (left + right) / 2;
    pinLabel.setAttribute('text-anchor', onLeft ? 'start' : 'end');
    pinLabel.setAttribute('x', onLeft ? '15' : '-15');
    pinLabel.setAttribute('y', y - top < 22 * scale ? '17' : '-10');
  }

  function select(place, { temporary = false, reveal = false } = {}) {
    const point = markers.get(place);
    if (!point) return;
    if (custom !== point) {
      if (custom) {
        custom.node.remove();
        custom.pin3d?.remove();
        custom.option.remove();
        markers.delete(custom.place);
      }
      custom = temporary ? point : undefined;
    }
    selector.disabled = false;
    selector.value = place;
    selected?.node.classList.remove('is-selected');
    selected?.pin3d?.setAttribute('aria-pressed', 'false');
    point.node.classList.add('is-selected');
    point.pin3d?.setAttribute('aria-pressed', 'true');
    pins.append(point.node);
    const name = Array.from(point.name);
    pinLabel.textContent = name.length > 10 ? name.slice(0, 9).join('') + '…' : point.name;
    point.symbol.append(pinLabel);
    if (selected !== point) {
      gallery.section.hidden = false;
      gallery.select(point);
    }
    selected = point;
    const [lon, lat] = point.coordinates;
    details.textContent = `${lat.toFixed(5)}°N，${lon.toFixed(5)}°E`;
    feedback.textContent = '';
    camera.setTarget(point.projected, reveal);
    scene?.render();
  }

  function add3dPin(marker) {
    const pin = element('button', 'map-spatial-pin');
    pin.type = 'button';
    pin.title = marker.name;
    pin.setAttribute('aria-label', marker.name);
    pin.setAttribute('aria-pressed', String(selected === marker));
    pin.classList.toggle('has-photos', marker.node.classList.contains('has-photos'));
    pin.addEventListener('click', () => select(marker.place));
    pins3d.append(pin);
    marker.pin3d = pin;
  }

  function addPoint(point, photoCount = 0) {
    const place = placeOf(point.coordinates);
    const existing = markers.get(place);
    if (existing) {
      if (photoCount) {
        existing.node.classList.add('has-photos');
        existing.pin3d?.classList.add('has-photos');
      }
      return existing;
    }
    const [x, y] = project(point.coordinates);
    if (x < 0 || x > width || y < 0 || y > height) return;
    const pin = svg('g', { class: 'map-pin', transform: `translate(${x.toFixed(2)} ${y.toFixed(2)})`, 'data-place': place });
    if (photoCount) pin.classList.add('has-photos');
    const title = svg('title');
    title.textContent = point.name;
    const symbol = svg('g');
    symbol.append(svg('circle', { class: 'map-pin-hit', r: pinHitRadius }),
      svg('circle', { class: 'map-pin-halo', r: 11 }),
      svg('circle', { class: 'map-pin-dot', r: 7.5 }));
    pin.append(title, symbol);
    const option = new Option(point.name, place);
    selector.add(option);
    pins.append(pin);
    const marker = { ...point, place, node: pin, symbol, projected: [x, y], option, eligible: canPhotograph(place, ring) };
    markers.set(place, marker);
    if (scene) add3dPin(marker);
    return marker;
  }
  for (const point of points.values()) addPoint(point);
  for (const item of photoPlaces) {
    if (!/^[0-9]{7}_[0-9]{6}$/.test(item.place) || !item.photo_count) continue;
    const coordinates = placeCoordinates(item.place);
    addPoint({ name: '校园照片点 · ' + coordinates[1].toFixed(4) + '，' + coordinates[0].toFixed(4), coordinates }, item.photo_count);
  }

  const camera = mapCamera(map, toolbar, element, (point, scale) => {
    let nearest, distance = pinHitRadius * scale;
    for (const marker of markers.values()) {
      const separation = Math.hypot(marker.projected[0] - point.x, marker.projected[1] - point.y);
      if (separation > distance) continue;
      nearest = marker;
      distance = separation;
    }
    if (nearest) return select(nearest.place);
    const position = unproject([point.x, point.y]);
    if (!inside(position, ring)) {
      feedback.textContent = '请选择校区边界内的拍摄点。';
      return;
    }
    const key = placeOf(position);
    if (markers.has(key)) return select(key);
    const marker = addPoint({ name: '自选拍摄点', coordinates: placeCoordinates(key) });
    if (marker) select(marker.place, { temporary: true });
  }, updateMarkers, signal);

  function switchView(use3d) {
    map.toggleAttribute('hidden', use3d);
    compass.hidden = toolbar.hidden = use3d;
    map3d.hidden = tools3d.hidden = !use3d;
    planar.setAttribute('aria-pressed', String(!use3d));
    spatial.setAttribute('aria-pressed', String(use3d));
    if (!use3d) return;
    try {
      if (!scene) {
        scene = createCampusMap3D(canvas, base, { project, width, height }, pinPosition => {
          for (const marker of markers.values()) {
            const [x, y] = pinPosition(marker.coordinates);
            const pin = marker.pin3d;
            pin.hidden = x < 10 || x > canvas.clientWidth - 10 || y < 10 || y > canvas.clientHeight - 10;
            pin.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%)`;
          }
        }, signal, () => {
          switchView(false);
          spatial.disabled = true;
          mapStatus.textContent = '3D 地图连接中断，已切回平面地图。';
        });
        for (const marker of markers.values()) add3dPin(marker);
        scene.setRoute(routePath);
      }
      scene.render();
    } catch (error) {
      switchView(false);
      spatial.disabled = true;
      mapStatus.textContent = error.message;
    }
  }
  planar.addEventListener('click', () => switchView(false));
  spatial.addEventListener('click', () => switchView(true));
  for (const [title, label, change] of [
    ['−', '缩小 3D 地图', () => scene?.zoom(.8)], ['+', '放大 3D 地图', () => scene?.zoom(1.25)],
    ['↶', '向左旋转 3D 地图', () => scene?.rotate(-.3)], ['↷', '向右旋转 3D 地图', () => scene?.rotate(.3)],
    ['全图', '还原 3D 视角', () => scene?.reset()],
  ]) {
    const control = makeButton(title, label);
    control.addEventListener('click', change);
    tools3d.append(control);
  }

  const caption = element('figcaption', 'map-credit', '候选步行路段以现场通行为准。 ');
  const source = element('a', '', '© OpenStreetMap contributors');
  source.href = 'https://www.openstreetmap.org/copyright';
  source.target = '_blank';
  source.rel = 'noopener noreferrer';
  caption.append(source);
  figure.append(caption);

  const label = element('label', 'panel-select map-select', '地图点位');
  label.append(selector);
  selector.addEventListener('change', () => select(selector.value, { reveal: true }));
  const information = element('aside', 'map-information');
  information.setAttribute('aria-label', '地图点位与地点照片');
  information.append(label, details, feedback, gallery.section);
  if (survey?.walking_places?.length > 1) {
    information.prepend(walkingRouteForm(survey.walking_places, request, signal, element, points => {
      routePath = points;
      routeLine.setAttribute('d', trace(points, project));
      if (points.length) {
        camera.reset();
        scene?.reset();
      }
      scene?.setRoute(points);
    }));
  }
  layout.append(tools, figure, information);
  const initial = [...markers.values()].find(point => point.eligible) || markers.values().next().value;
  if (initial) select(initial.place);
  else selector.disabled = true;
  return layout;
}

function walkingRouteForm(places, request, signal, element, draw) {
  const form = element('form', 'map-walking-form');
  const title = element('h3', '', '步行路线');
  const fields = element('div', 'map-walking-fields');
  const selectPlace = caption => {
    const label = element('label', 'panel-select map-select', caption);
    const select = element('select');
    select.required = true;
    select.add(new Option('选择' + caption, ''));
    for (const place of places) select.add(new Option(place.name, place.id));
    label.append(select);
    fields.append(label);
    return select;
  };
  const start = selectPlace('起点'), end = selectPlace('终点');
  const submit = element('button', 'outline-button', '查看路线');
  const status = element('p', 'map-point-details');
  status.setAttribute('role', 'status');
  const steps = element('ol', 'map-walking-steps');
  fields.append(submit);
  form.append(title, fields, status, steps);
  let pending;
  form.addEventListener('change', () => {
    pending?.abort();
    submit.disabled = false;
    status.textContent = '';
    steps.replaceChildren();
    draw([]);
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    if (start.value === end.value) {
      status.textContent = '请选择不同的起点和终点。';
      return;
    }
    pending?.abort();
    pending = new AbortController();
    const current = AbortSignal.any([pending.signal, signal]);
    submit.disabled = true;
    status.textContent = '正在规划…';
    try {
      const route = await request('/v1/campus-view/resolve', { method: 'POST',
        data: { start: start.value, destination: end.value }, signal: current });
      if (current.aborted) return;
      if (route.status !== 'walking_route') {
        status.textContent = route.status === 'same_place' ? '两处位于同一路网点位附近。' : '这两个地点之间暂时没有可显示的路线。';
        return;
      }
      draw(route.path);
      status.textContent = `${route.start.name} → ${route.destination.name} · 约 ${route.distance_m} 米`;
      steps.replaceChildren(...route.steps.map(step =>
        element('li', '', `沿${step.road}向${step.direction}走约 ${step.distance_m} 米`)));
      if (route.destination.note) steps.append(element('li', '', route.destination.note));
    } catch (error) {
      if (!current.aborted) status.textContent = error.message;
    } finally {
      if (!current.aborted) submit.disabled = false;
    }
  });
  return form;
}
