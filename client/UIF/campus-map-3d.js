import { buildCampusScene } from './campus-map-scene.js';

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const initialView = { yaw: -.90, tilt: .58, zoom: 1, x: 0, y: 0 };

const vertexShader = `
  attribute vec3 position;
  attribute vec3 color;
  uniform vec2 rotation, pitch, halfSize, pan;
  uniform float zoom;
  varying vec3 surface;
  void main() {
    float x = rotation.x * position.x - rotation.y * position.z;
    float z = rotation.y * position.x + rotation.x * position.z;
    vec2 screen = vec2(x, position.y * pitch.x - z * pitch.y) * zoom + pan;
    float depth = (-position.y * pitch.y - z * pitch.x) / 900.0;
    gl_Position = vec4(screen / halfSize, depth, 1.0);
    surface = color;
  }
`;
const fragmentShader = `
  precision mediump float;
  varying vec3 surface;
  void main() { gl_FragColor = vec4(surface, 1.0); }
`;

function renderProgram(gl) {
  const program = gl.createProgram();
  const shaders = [[gl.VERTEX_SHADER, vertexShader], [gl.FRAGMENT_SHADER, fragmentShader]].map(([type, source]) => {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    gl.attachShader(program, shader);
    return shader;
  });
  gl.linkProgram(program);
  for (const shader of shaders) gl.deleteShader(shader);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    gl.deleteProgram(program);
    throw new Error('无法初始化 3D 地图。');
  }
  gl.useProgram(program);
  return program;
}

const cross = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);

function triangulate(ring) {
  const area = ring.reduce((sum, point, index) => sum + cross([0, 0], point, ring[(index + 1) % ring.length]), 0);
  const outline = area > 0 ? [...ring] : ring.slice().reverse();
  const triangles = [];
  while (outline.length >= 3) {
    const ear = outline.findIndex((point, index) => {
      const before = outline.at(index - 1), after = outline[(index + 1) % outline.length];
      if (cross(before, point, after) <= 1e-7) return false;
      return !outline.some(other => other !== before && other !== point && other !== after &&
        cross(before, point, other) >= 0 && cross(point, after, other) >= 0 && cross(after, before, other) >= 0);
    });
    if (ear < 0) {
      const straight = outline.findIndex((point, index) =>
        Math.abs(cross(outline.at(index - 1), point, outline[(index + 1) % outline.length])) <= 1e-7);
      if (straight < 0) break;
      outline.splice(straight, 1);
      continue;
    }
    triangles.push(outline.at(ear - 1), outline[ear], outline[(ear + 1) % outline.length]);
    outline.splice(ear, 1);
  }
  return triangles;
}

function mesh(project, width, height) {
  const vertices = [];
  const local = position => {
    const [x, y] = project(position);
    return [x - width / 2, y - height / 2];
  };
  function vertex([x, z], height, color) {
    vertices.push(x, height, z, ...color);
  }
  function surface(ring, elevation, color) {
    for (const point of triangulate(ring)) vertex(point, elevation, color);
  }
  function wall(a, b, bottom, top, color) {
    for (const [point, y] of [[a, bottom], [b, bottom], [b, top], [a, bottom], [b, top], [a, top]]) {
      vertex(point, y, color);
    }
  }
  function raised(ring, base, top, sideColor, topColor) {
    surface(ring, top, topColor);
    for (let index = 0; index < ring.length; index++) {
      const a = ring[index], b = ring[(index + 1) % ring.length];
      const angle = Math.atan2(b[1] - a[1], b[0] - a[0]);
      const tone = .75 + .23 * Math.abs(Math.cos(angle + .65));
      wall(a, b, base, top, sideColor.map(value => value * tone));
    }
  }
  function ribbon(path, thickness, elevation, color) {
    for (let index = 1; index < path.length; index++) {
      const [x, z] = path[index - 1], [endX, endZ] = path[index];
      const dx = endX - x, dz = endZ - z;
      const length = Math.hypot(dx, dz);
      if (!length) continue;
      const sideX = -dz / length * thickness, sideZ = dx / length * thickness;
      const a = [x + sideX, z + sideZ], b = [x - sideX, z - sideZ];
      const c = [endX + sideX, endZ + sideZ], d = [endX - sideX, endZ - sideZ];
      for (const point of [a, b, c, b, d, c]) vertex(point, elevation, color);
    }
  }
  const line = (coordinates, thickness, elevation, color) => ribbon(coordinates.map(local), thickness, elevation, color);
  return { vertices, local, raised, surface, wall, vertex, ribbon, line };
}

export function createCampusMap3D(canvas, base, projection, onFrame, signal, onLost) {
  const gl = canvas.getContext('webgl', { alpha: false, antialias: true });
  if (!gl) throw new Error('当前浏览器无法打开 3D 地图。');
  const program = renderProgram(gl);
  const position = gl.getAttribLocation(program, 'position');
  const color = gl.getAttribLocation(program, 'color');
  const uniforms = Object.fromEntries(['rotation', 'pitch', 'halfSize', 'pan', 'zoom']
    .map(name => [name, gl.getUniformLocation(program, name)]));
  const view = { ...initialView };
  const scene = mesh(projection.project, projection.width, projection.height);
  buildCampusScene(scene, base, projection.width, projection.height);
  const world = gl.createBuffer(), route = gl.createBuffer();
  let pathSize = 0, frame = 0;
  gl.bindBuffer(gl.ARRAY_BUFFER, world);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(scene.vertices), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(position);
  gl.enableVertexAttribArray(color);
  gl.enable(gl.DEPTH_TEST);
  gl.clearColor(.86, .87, .87, 1);

  function draw(buffer, vertices) {
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.vertexAttribPointer(position, 3, gl.FLOAT, false, 24, 0);
    gl.vertexAttribPointer(color, 3, gl.FLOAT, false, 24, 12);
    gl.drawArrays(gl.TRIANGLES, 0, vertices);
  }

  function paint() {
    frame = 0;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h || gl.isContextLost()) return;
    const resolution = Math.min(window.devicePixelRatio || 1, 2);
    const pixels = [Math.round(w * resolution), Math.round(h * resolution)];
    if (canvas.width !== pixels[0]) canvas.width = pixels[0];
    if (canvas.height !== pixels[1]) canvas.height = pixels[1];
    const scale = view.zoom * Math.min(w / projection.width, h / projection.height) * 1.19;
    const cosine = Math.cos(view.yaw), sine = Math.sin(view.yaw);
    const pitch = [Math.cos(view.tilt), Math.sin(view.tilt)];
    gl.viewport(0, 0, ...pixels);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.uniform2f(uniforms.rotation, cosine, sine);
    gl.uniform2f(uniforms.pitch, ...pitch);
    gl.uniform2f(uniforms.halfSize, w / 2, h / 2);
    gl.uniform2f(uniforms.pan, view.x, -view.y);
    gl.uniform1f(uniforms.zoom, scale);
    draw(world, scene.vertices.length / 6);
    if (pathSize) draw(route, pathSize);
    onFrame(coordinate => {
      const [x, z] = scene.local(coordinate);
      return [w / 2 + (cosine * x - sine * z) * scale + view.x,
        h / 2 + ((sine * x + cosine * z) * pitch[1] - 3 * pitch[0]) * scale + view.y];
    });
  }

  function render() {
    if (!signal.aborted && !frame) frame = requestAnimationFrame(paint);
  }

  function zoom(factor) { view.zoom = clamp(view.zoom * factor, .65, 5); render(); }
  function rotate(dx, dy = 0) { view.yaw += dx; view.tilt = clamp(view.tilt + dy, .32, 1.52); render(); }
  function reset() { Object.assign(view, initialView); render(); }
  function setRoute(points) {
    const overlay = mesh(projection.project, projection.width, projection.height);
    overlay.line(points, 2.7, 1.15, [.98, .95, .83]);
    overlay.line(points, 1.65, 1.22, [.85, .32, .09]);
    gl.bindBuffer(gl.ARRAY_BUFFER, route);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(overlay.vertices), gl.DYNAMIC_DRAW);
    pathSize = overlay.vertices.length / 6;
    render();
  }

  const pointers = new Map();
  const listen = (type, callback, options = {}) => canvas.addEventListener(type, callback, { ...options, signal });
  listen('pointerdown', event => {
    if (![0, 2].includes(event.button) || pointers.size >= 2) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY, pan: event.button === 2 || event.shiftKey });
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add('is-panning');
  });
  listen('pointermove', event => {
    const point = pointers.get(event.pointerId);
    if (!point) return;
    const dx = event.clientX - point.x, dy = event.clientY - point.y;
    const other = [...pointers.values()].find(value => value !== point);
    if (other) {
      const before = Math.hypot(point.x - other.x, point.y - other.y);
      const after = Math.hypot(event.clientX - other.x, event.clientY - other.y);
      if (before > 1) zoom(after / before);
    }
    if (point.pan || other) { view.x += other ? dx / 2 : dx; view.y += other ? dy / 2 : dy; render(); }
    else rotate(-dx * .008, -dy * .008);
    point.x = event.clientX;
    point.y = event.clientY;
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) listen(type, event => {
    pointers.delete(event.pointerId);
    if (!pointers.size) canvas.classList.remove('is-panning');
  });
  listen('contextmenu', event => event.preventDefault());
  listen('wheel', event => {
    if (!event.deltaY) return;
    event.preventDefault();
    zoom(event.deltaY < 0 ? 1.16 : 1 / 1.16);
  }, { passive: false });
  listen('keydown', event => {
    const angles = { ArrowLeft: [-.16, 0], ArrowRight: [.16, 0], ArrowUp: [0, -.11], ArrowDown: [0, .11] };
    if (Object.hasOwn(angles, event.key)) rotate(...angles[event.key]);
    else if (event.key === '+' || event.key === '=') zoom(1.25);
    else if (event.key === '-') zoom(.8);
    else if (event.key === '0') reset();
    else return;
    event.preventDefault();
  });
  listen('webglcontextlost', onLost);
  const observer = new ResizeObserver(render);
  observer.observe(canvas);
  signal.addEventListener('abort', () => {
    observer.disconnect();
    cancelAnimationFrame(frame);
    gl.deleteBuffer(world);
    gl.deleteBuffer(route);
    gl.deleteProgram(program);
  }, { once: true });
  render();
  return { render, reset, zoom, rotate, setRoute };
}
