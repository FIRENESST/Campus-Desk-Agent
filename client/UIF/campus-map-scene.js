const TAU = Math.PI * 2;
const tint = (color, light) => color.map(channel => channel * light);
const offset = ([x, y], [dx, dy], amount = 1) => [x + dx * amount, y + dy * amount];
const between = (a, b, t) => offset(a, [b[0] - a[0], b[1] - a[1]], t);

const materials = {
  stone: { wall: [.87, .84, .76], roof: [.82, .81, .76], glass: [.45, .53, .55] },
  brick: { wall: [.69, .41, .34], roof: [.62, .30, .25], glass: [.37, .45, .46] },
  tile: { wall: [.81, .77, .64], roof: [.72, .31, .24], glass: [.45, .51, .51] },
  glass: { wall: [.71, .74, .73], roof: [.72, .76, .76], glass: [.36, .48, .54], curtain: true },
  studio: { wall: [.79, .79, .71], roof: [.78, .75, .67], glass: [.37, .48, .51], bands: true },
};

const buildingProfiles = new Map([
  [523569201, { height: 30, material: 'glass', roof: 'library' }],        // 大阅城
  [523220221, { height: 23, material: 'glass' }],                         // 学生活动中心
  [523219791, { height: 33, material: 'brick' }],                         // 新知楼
  [255744677, { height: 34, material: 'glass', roof: 'terrace' }],        // 博学楼
  [198238561, { height: 13, material: 'tile', roof: 'gable' }],           // 立德楼
  [768059205, { height: 34, material: 'stone', roof: 'pavilion' }],        // 明德楼
  [772369994, { height: 14, material: 'tile', roof: 'gable' }],           // 主楼
  [523569206, { height: 16, material: 'glass' }],                         // 广告博物馆
  [523569200, { height: 16, material: 'brick' }],
  [523569198, { height: 19, material: 'glass' }],
  [523254357, { height: 12, material: 'tile', roof: 'gable' }],
  [772351879, { height: 11, material: 'tile', roof: 'gable' }],
  [783786105, { height: 14, material: 'studio' }],
  [783788781, { height: 17, material: 'studio' }],
  [783787491, { height: 38, material: 'brick' }],
  [783787656, { height: 17, material: 'studio' }],
  [783788384, { height: 8, material: 'tile', roof: 'gable' }],
  [849997694, { height: 43, material: 'brick' }],                          // 47 号研究生公寓
  [198238556, { height: 18, material: 'tile', roof: 'gable' }],
  [198238557, { height: 16, material: 'tile', roof: 'gable' }],
  [198238565, { height: 16, material: 'tile', roof: 'gable' }],
]);

function center(ring) {
  return ring.reduce(([x, y], p) => [x + p[0] / ring.length, y + p[1] / ring.length], [0, 0]);
}

function bounds(ring) {
  const x = ring.map(p => p[0]), y = ring.map(p => p[1]);
  return { left: Math.min(...x), right: Math.max(...x), top: Math.min(...y), bottom: Math.max(...y) };
}

function contains([x, y], ring) {
  let inside = false;
  for (let i = 0; i < ring.length; i++) {
    const [a, b] = ring[i], [c, d] = ring[(i + 1) % ring.length];
    if ((b > y) !== (d > y) && x < a + (c - a) * (y - b) / (d - b)) inside = !inside;
  }
  return inside;
}

function nearSegment([x, y], [[a, b], [c, d]], radius) {
  const dx = c - a, dy = d - b, length = dx * dx + dy * dy;
  const t = length ? Math.min(1, Math.max(0, ((x - a) * dx + (y - b) * dy) / length)) : 0;
  return (x - a - dx * t) ** 2 + (y - b - dy * t) ** 2 < radius ** 2;
}

function rectangle(point, width, depth, angle = 0) {
  const right = [Math.cos(angle), Math.sin(angle)], up = [-right[1], right[0]];
  return [[-.5, -.5], [.5, -.5], [.5, .5], [-.5, .5]].map(([x, y]) =>
    offset(offset(point, right, width * x), up, depth * y));
}

function circle(point, radius, count = 12, ratio = 1) {
  return Array.from({ length: count }, (_, i) => {
    const angle = i * TAU / count;
    return offset(point, [Math.cos(angle) * radius, Math.sin(angle) * radius * ratio]);
  });
}

function polygonRings({ type, coordinates }, project) {
  const polygons = type === 'MultiPolygon' ? coordinates : type === 'Polygon' ? [coordinates] : [];
  return polygons.map(([ring]) => ring.slice(0, -1).map(project)).filter(ring => ring.length >= 3);
}

function shadow(scene, ring, height, color, elevation = .37) {
  const displaced = ring.map(point => offset(point, [-.43, -.47], height));
  scene.surface(displaced, elevation, color);
  for (let i = 0; i < ring.length; i++) {
    const next = (i + 1) % ring.length;
    scene.surface([ring[i], ring[next], displaced[next], displaced[i]], elevation, color);
  }
}

function facade(scene, outline, height, material) {
  const clockwise = outline.reduce((sum, point, i) => {
    const next = outline[(i + 1) % outline.length];
    return sum + point[0] * next[1] - next[0] * point[1];
  }, 0) >= 0;
  for (let i = 0; i < outline.length; i++) {
    const a = outline[i], b = outline[(i + 1) % outline.length];
    const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (length < .1) continue;
    const side = (clockwise ? 1 : -1) / length;
    const normal = [(b[1] - a[1]) * side, (a[0] - b[0]) * side];
    const light = .73 + .27 * Math.max(0, normal[0] * .55 + normal[1] * .83);
    scene.wall(a, b, 0, height, tint(material.wall, light));
    const strip = (left, right, bottom, top, color) => scene.wall(
      offset(between(a, b, left), normal, .13), offset(between(a, b, right), normal, .13),
      bottom, top, tint(color, light));
    strip(0, 1, 0, Math.min(1.5, height * .18), tint(material.wall, .84));
    strip(0, 1, height - .54, height, tint(material.roof, 1.08));
    if (length < 3.8 || height < 4) continue;
    const floors = Math.max(1, Math.round((height - 1.5) / 2.9));
    const floorHeight = (height - 1.5) / floors;
    const bays = Math.max(1, Math.floor(length / (material.curtain ? 2.65 : 4.2)));
    for (let floor = 0; floor < floors; floor++) {
      const base = 1 + floor * floorHeight;
      if (material.bands) {
        strip(.055, .945, base + floorHeight * .22, base + floorHeight * .69, material.glass);
        continue;
      }
      for (let bay = 0; bay < bays; bay++) {
        const margin = material.curtain ? .105 : .21;
        const variation = .91 + .17 * ((bay * 13 + floor * 7 + i) % 5) / 4;
        strip((bay + margin) / bays, (bay + 1 - margin) / bays,
          base + floorHeight * (material.curtain ? .11 : .29),
          base + floorHeight * (material.curtain ? .87 : .74), tint(material.glass, variation));
      }
    }
  }
}

function pitchedRoof(scene, ring, height, color) {
  const [a, b, c, d] = ring;
  const longEdge = Math.hypot(a[0] - b[0], a[1] - b[1]) > Math.hypot(a[0] - d[0], a[1] - d[1]);
  const [p, q, r, s] = longEdge ? [a, b, c, d] : [d, a, b, c];
  const first = between(p, s, .5), last = between(q, r, .5);
  const ridge = height + Math.min(3.4, Math.hypot(p[0] - s[0], p[1] - s[1]) * .31);
  for (const [face, shade] of [[[p, q, last, first], 1], [[s, r, last, first], .79]]) {
    for (const i of [0, 1, 2, 0, 2, 3]) scene.vertex(face[i], i >= 2 ? ridge : height, tint(color, shade));
  }
  for (const [left, right, apex] of [[p, s, first], [q, r, last]]) {
    scene.vertex(left, height, tint(color, .87));
    scene.vertex(right, height, tint(color, .87));
    scene.vertex(apex, ridge, tint(color, .87));
  }
}

function roof(scene, outline, height, color, style) {
  scene.surface(outline, height, color);
  if (style === 'gable' && outline.length === 4) {
    pitchedRoof(scene, outline, height, color);
    return;
  }
  scene.ribbon([...outline, outline[0]], .25, height + .12, tint(color, .89));
  const point = center(outline);
  if (style === 'library') {
    const extent = bounds(outline);
    const glass = rectangle(offset(point, [-3, 0]), (extent.right - extent.left) * .42, 6.5, -.08);
    scene.raised(glass, height, height + 1.5, [.36, .47, .51], [.40, .54, .59]);
    for (let i = -3; i <= 3; i++) {
      const bar = rectangle(offset(point, [i * 3.6 - 3, 0]), .30, 6.4, -.08);
      scene.surface(bar, height + 1.55, [.67, .73, .74]);
    }
  } else if (style === 'pavilion') {
    scene.raised(rectangle(point, 10, 7, -.08), height, height + 2.3, [.62, .57, .48], [.67, .61, .50]);
    const cap = rectangle(point, 15, 10, -.08);
    scene.raised(cap, height + 2.3, height + 2.9, [.39, .33, .29], [.45, .39, .34]);
    pitchedRoof(scene, cap, height + 2.9, [.43, .35, .31]);
  } else {
    const detail = rectangle(point, style === 'terrace' ? 8 : 3.7, 2.6, -.08);
    if (detail.every(p => contains(p, outline))) {
      scene.raised(detail, height, height + 1, tint(color, .86), tint(color, 1.05));
    }
  }
}

function city(scene, boundary, width, height) {
  const ext = bounds(boundary);
  scene.surface(rectangle([0, 0], width * 2.3, height * 2.65), -1.4, [.86, .86, .87]);
  for (const [point, size] of [
    [[ext.left - 15, 0], [16, height * 2.4]], [[ext.right + 15, 0], [16, height * 2.4]],
    [[0, ext.top - 16], [width * 2.2, 17]], [[0, ext.bottom + 15], [width * 2.2, 17]],
  ]) {
    scene.surface(rectangle(point, ...size), -1.05, [.79, .80, .80]);
  }
  for (let row = -3; row <= 3; row++) {
    for (let col = -4; col <= 4; col++) {
      const seed = ((row + 7) * 19 + (col + 7) * 11) % 17;
      const [x, y] = [col * 60 + seed - 8, row * 57 + seed % 4 * 3];
      if (x > ext.left - 41 && x < ext.right + 41 && y > ext.top - 37 && y < ext.bottom + 37) continue;
      const block = rectangle([x, y], 24 + seed % 9 * 2.4, 12 + seed % 5 * 2.5, -.08);
      const levels = seed < 3 ? 34 + seed * 7 : 5 + seed * 1.2;
      shadow(scene, block, levels, [.73, .74, .76], -1);
      scene.raised(block, -.99, levels, [.79, .79, .81], [.93, .93, .93]);
      if (seed % 3 !== 1) {
        const annex = rectangle([x + seed % 6, y + 23], 16 + seed % 7 * 2, 10, -.08);
        scene.raised(annex, -.99, 4 + seed % 6, [.81, .81, .82], [.94, .94, .93]);
      }
    }
  }
  scene.raised(boundary, -1.35, 0, [.64, .69, .60], [.70, .80, .62]);
}

function field(scene, areas) {
  const track = areas.find(area => area.kind === 'track');
  const pitch = areas.find(area => area.kind === 'pitch');
  if (!track || !pitch) return;
  scene.surface(track.ring, .19, [.73, .31, .25]);
  const point = center(track.ring);
  for (let i = 0; i < 5; i++) {
    const lane = track.ring.map(p => between(p, point, .026 + i * .029));
    scene.ribbon([...lane, lane[0]], .17, .23, [.96, .79, .68]);
  }
  scene.surface(track.ring.map(p => between(p, point, .175)), .25, [.46, .62, .36]);
  const corners = pitch.ring;
  scene.surface(corners, .27, [.37, .57, .29]);
  if (corners.length !== 4) return;
  const [a, b, c, d] = corners;
  const white = [.92, .94, .83];
  for (let index = 0; index < 9; index += 2) {
    const l = index / 9, r = (index + 1) / 9;
    scene.surface([between(a, d, l), between(b, c, l), between(b, c, r), between(a, d, r)], .30, [.43, .63, .33]);
  }
  scene.ribbon([a, b, c, d, a], .23, .33, white);
  const middle = [between(a, d, .5), between(b, c, .5)];
  scene.ribbon(middle, .20, .33, white);
  const central = circle(center(middle), 4.7, 18);
  scene.ribbon([...central, central[0]], .20, .33, white);
  for (const [left, right, innerLeft, innerRight] of [[a, b, d, c], [d, c, a, b]]) {
    for (const [start, depth] of [[.18, .15], [.34, .065]]) {
      const nearLeft = between(left, right, start), nearRight = between(left, right, 1 - start);
      scene.ribbon([nearLeft, between(nearLeft, between(innerLeft, innerRight, start), depth),
        between(nearRight, between(innerLeft, innerRight, 1 - start), depth), nearRight], .20, .33, white);
    }
  }
}

function vegetation(scene, boundary, roads, footprints, areas) {
  const trees = [];
  const free = p => contains(p, boundary) && !footprints.some(ring => contains(p, ring)) &&
    !areas.some(area => contains(p, area.ring)) && !roads.some(road => nearSegment(p, road.points, road.width + 2.3)) &&
    !trees.some(other => Math.hypot(p[0] - other[0], p[1] - other[1]) < 5.8);
  function plant(point, seed) {
    if (!free(point)) return;
    trees.push(point);
    const radius = 1.5 + seed % 7 * .22, height = 3.4 + radius * 1.5;
    const colors = [[.30, .46, .23], [.35, .52, .25], [.39, .56, .26], [.41, .54, .29]];
    const foliage = colors[seed % colors.length];
    scene.surface(circle(offset(point, [-.60, -.40], height * .55), radius * 1.15, 7, .71), .44, [.53, .63, .45]);
    scene.raised(rectangle(point, .48, .48), 0, height * .65, [.43, .35, .27], [.43, .35, .27]);
    const skirt = circle(point, radius, 8);
    for (let i = 0; i < skirt.length; i++) {
      const a = skirt[i], b = skirt[(i + 1) % skirt.length];
      const shade = tint(foliage, .77 + i * .041);
      for (const p of [a, b]) scene.vertex(p, height * .63, shade);
      scene.vertex(offset(point, [-.34, -.18]), height, tint(foliage, 1.08));
      for (const p of [a, b]) scene.vertex(p, height * .63, shade);
      scene.vertex(point, height * .39, tint(shade, .83));
    }
  }
  for (const { points: [a, b], width, primary } of roads) {
    const length = Math.hypot(a[0] - b[0], a[1] - b[1]);
    if (length < 6) continue;
    const normal = [(a[1] - b[1]) / length, (b[0] - a[0]) / length];
    for (let d = 4; d < length - 2; d += primary ? 6.5 : 9) {
      const point = between(a, b, d / length);
      for (const side of [-1, 1]) {
        plant(offset(point, normal, side * (width + 4)), Math.abs(Math.round(point[0] * 19 + point[1] * 7 + side)));
      }
    }
  }
  const ext = bounds(boundary);
  for (let x = ext.left + 7; x < ext.right; x += 15) {
    for (let y = ext.top + 7; y < ext.bottom; y += 16) {
      const seed = Math.abs(Math.round(x * 39 + y * 17));
      if (seed % 5 > 1) plant([x + seed % 7, y + seed % 5], seed);
    }
  }
}

export function buildCampusScene(scene, base, width, height) {
  const outline = base.features.find(feature => feature.properties?.kind === 'campus_boundary');
  const boundary = outline && polygonRings(outline.geometry, scene.local)[0];
  if (!boundary) throw new Error('校园底图缺少校区范围。');
  const roads = [], areas = [], footprints = [];
  city(scene, boundary, width, height);
  for (const { id, geometry, properties } of base.features) {
    const kind = properties?.kind;
    if (kind === 'walkway_candidate' && geometry.type === 'LineString') {
      const primary = properties.name === '白杨大道';
      const width = primary ? 3.2 : 1.85;
      const points = geometry.coordinates.map(scene.local);
      for (let i = 1; i < points.length; i++) roads.push({ points: points.slice(i - 1, i + 1), width, primary });
      scene.ribbon(points, width + .55, .43, [.80, .80, .73]);
      scene.ribbon(points, width, .49, primary ? [.69, .71, .68] : [.88, .87, .81]);
    }
    for (const ring of polygonRings(geometry, scene.local)) {
      if (kind === 'facility_area') {
        const leisure = properties.tags?.leisure;
        areas.push({ ring, kind: leisure });
        if (leisure === 'park') scene.surface(ring, .22, [.81, .80, .70]);
        if (leisure === 'bleachers') scene.raised(ring, .15, 2.2, [.62, .64, .58], [.76, .76, .68]);
      }
      if (kind !== 'building') continue;
      footprints.push(ring);
      const number = Number(String(id).split('-').at(-1)) || 0;
      if ([783282804, 775451887, 996565170].includes(number)) {
        const point = center(ring);
        scene.surface(ring, .21, [.85, .83, .71]);
        if (number === 783282804) scene.surface(circle(point, 4.4), .24, [.39, .65, .68]);
        else scene.raised(rectangle(point, 1.5, 1.5), .22, 3.3, [.53, .49, .39], [.67, .62, .51]);
        continue;
      }
      const levels = Number(properties.tags?.['building:levels']);
      const shade = number % 4;
      const profile = buildingProfiles.get(number) || {
        height: levels > 0 ? Math.min(levels * 2.1, 48) : 6.5 + shade * 1.95,
        material: ['stone', 'tile', 'stone', 'brick'][shade],
        roof: shade === 1 || shade === 3 ? 'gable' : 'terrace',
      };
      const material = materials[profile.material];
      shadow(scene, ring, profile.height, [.46, .56, .39]);
      facade(scene, ring, profile.height, material);
      roof(scene, ring, profile.height, material.roof, profile.roof);
    }
  }
  field(scene, areas);
  vegetation(scene, boundary, roads, footprints, areas);
}
