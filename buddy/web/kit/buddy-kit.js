// buddy-kit: small helpers so a model-written visual stays short and looks consistent.
//
// 3D:  const s = kit.scene3d();  s.add(kit.part(geometry, "#9aa3ad"));
//      kit.label(s, mesh, "Turbine", { color: "blue", offset: [1.5, 1, 0] });  s.start();
// 2D:  <div class="buddy-card"> ... </div> plus kit.connect(fromElement, toElement)
// Diagrams: await kit.mermaid(element, "flowchart LR; A-->B")

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";

const NOTE_COLORS = ["blue", "red", "green", "yellow", "purple", "pink"];

function toVector3(value) {
  if (value instanceof THREE.Vector3) return value.clone();
  if (Array.isArray(value)) return new THREE.Vector3(value[0] || 0, value[1] || 0, value[2] || 0);
  return new THREE.Vector3();
}

function anchorWorldPoint(target) {
  // A mesh/group anchors at its bounding-box center; arrays/vectors are used as-is.
  if (target && target.isObject3D) {
    target.updateWorldMatrix(true, true);
    const bounds = new THREE.Box3().setFromObject(target);
    if (!bounds.isEmpty()) return bounds.getCenter(new THREE.Vector3());
    return target.getWorldPosition(new THREE.Vector3());
  }
  return toVector3(target);
}

/** A mesh with the illustrated look: flat shading plus faint dark edges. */
function part(geometry, color = "#b8bec6", options = {}) {
  const material = new THREE.MeshStandardMaterial({
    color,
    roughness: options.roughness ?? 0.65,
    metalness: options.metalness ?? 0.1,
    flatShading: options.flatShading ?? true,
    transparent: options.opacity !== undefined,
    opacity: options.opacity ?? 1,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  if (options.edges !== false) {
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry, 30),
      new THREE.LineBasicMaterial({ color: 0x1f2937, transparent: true, opacity: 0.28 })
    );
    mesh.add(edges);
  }
  if (options.position) mesh.position.copy(toVector3(options.position));
  if (options.rotation) mesh.rotation.set(...options.rotation);
  if (options.scale) mesh.scale.copy(toVector3(options.scale));
  return mesh;
}

/** A chunky arrow (cylinder shaft + cone head) from one point to another. */
function arrow(from, to, color = "#6aa9c9", thickness = 0.06) {
  const start = toVector3(from);
  const end = toVector3(to);
  const direction = end.clone().sub(start);
  const length = direction.length();
  const headLength = Math.min(length * 0.3, thickness * 6);
  const group = new THREE.Group();
  const material = new THREE.MeshStandardMaterial({ color, roughness: 0.5, flatShading: true });
  const shaft = new THREE.Mesh(new THREE.CylinderGeometry(thickness, thickness, length - headLength, 12), material);
  shaft.position.y = (length - headLength) / 2;
  const head = new THREE.Mesh(new THREE.ConeGeometry(thickness * 2.6, headLength, 16), material);
  head.position.y = length - headLength / 2;
  shaft.castShadow = head.castShadow = true;
  group.add(shaft, head);
  group.position.copy(start);
  group.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize());
  return group;
}

/**
 * Transparent 3D scene that fills the visual window, with soft lights, a shadow
 * catcher under the model, orbit controls (drag to rotate, wheel to zoom) and
 * sticky-note labels.
 */
function scene3d(options = {}) {
  const container = options.container || document.body;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(options.fov ?? 35, 1, 0.01, 2000);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.style.cssText = "position:fixed;inset:0;";
  container.appendChild(renderer.domElement);

  const labelRenderer = new CSS2DRenderer();
  labelRenderer.domElement.style.cssText = "position:fixed;inset:0;pointer-events:none;";
  container.appendChild(labelRenderer.domElement);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 1.6));
  const sunLight = new THREE.DirectionalLight(0xffffff, 1.8);
  sunLight.castShadow = true;
  sunLight.shadow.mapSize.set(2048, 2048);
  scene.add(sunLight);

  const shadowCatcher = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1),
    new THREE.ShadowMaterial({ opacity: options.shadowOpacity ?? 0.18 })
  );
  shadowCatcher.rotation.x = -Math.PI / 2;
  shadowCatcher.receiveShadow = true;
  scene.add(shadowCatcher);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.autoRotate = options.autoRotate ?? true;
  controls.autoRotateSpeed = options.autoRotateSpeed ?? 0.8;
  controls.addEventListener("start", () => { controls.autoRotate = false; });

  const content = new THREE.Group();
  scene.add(content);
  // Labels without an explicit offset are placed when the scene starts (bounds known).
  const pendingLabels = [];
  const placedNotes = [];
  const frameCallbacks = [];
  const clock = new THREE.Clock();
  let hasFramed = false;

  function resize() {
    const width = window.innerWidth;
    const height = window.innerHeight;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    renderer.domElement.style.width = width + "px";
    renderer.domElement.style.height = height + "px";
    labelRenderer.setSize(width, height);
  }
  window.addEventListener("resize", resize);
  resize();

  /** Points the camera at everything added so far (call again after big changes). */
  function frame(viewDirection = options.view ?? [1, 0.7, 1.25]) {
    const bounds = new THREE.Box3().setFromObject(content);
    if (bounds.isEmpty()) return;
    const center = bounds.getCenter(new THREE.Vector3());
    const size = bounds.getSize(new THREE.Vector3());
    const radius = Math.max(size.length() / 2, 0.001);
    // Fit the bounding sphere into the narrower field of view, a bit tighter than the sphere.
    const verticalHalfFov = THREE.MathUtils.degToRad(camera.fov / 2);
    const horizontalHalfFov = Math.atan(Math.tan(verticalHalfFov) * camera.aspect);
    const distance = radius / Math.sin(Math.min(verticalHalfFov, horizontalHalfFov)) * (options.zoomOut ?? 0.95);
    camera.position.copy(center).add(toVector3(viewDirection).normalize().multiplyScalar(distance));
    camera.near = distance / 100;
    camera.far = distance * 10;
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();

    shadowCatcher.position.set(center.x, bounds.min.y - radius * 0.02, center.z);
    shadowCatcher.scale.setScalar(radius * 6);
    sunLight.position.copy(center).add(new THREE.Vector3(radius * 2, radius * 4, radius * 1.5));
    sunLight.target.position.copy(center);
    sunLight.target.updateMatrixWorld();
    const shadowCamera = sunLight.shadow.camera;
    shadowCamera.left = shadowCamera.bottom = -radius * 2.5;
    shadowCamera.right = shadowCamera.top = radius * 2.5;
    shadowCamera.near = radius * 0.1;
    shadowCamera.far = radius * 12;
    shadowCamera.updateProjectionMatrix();
    hasFramed = true;
  }

  function render() {
    const elapsedSeconds = clock.getElapsedTime();
    for (const callback of frameCallbacks) callback(elapsedSeconds);
    controls.update();
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
  }

  function placePendingLabels() {
    const bounds = new THREE.Box3().setFromObject(content);
    if (bounds.isEmpty()) return;
    const center = bounds.getCenter(new THREE.Vector3());
    const radius = bounds.getSize(new THREE.Vector3()).length() / 2;
    pendingLabels.forEach((pending, index) => {
      // Push each note outward from the model's center, alternating up/down a little.
      const outward = pending.anchor.clone().sub(center);
      outward.y = 0;
      if (outward.lengthSq() < 1e-6) outward.set(index % 2 ? -1 : 1, 0, 0);
      outward.normalize().multiplyScalar(radius * 0.55);
      outward.y = radius * (index % 2 ? 0.18 : 0.4);
      pending.place(pending.anchor.clone().add(outward));
    });
    pendingLabels.length = 0;
  }

  /** Nudges notes apart on screen so none overlap (runs once after framing). */
  function separateNotes() {
    if (placedNotes.length < 2) return;
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
    const bounds = new THREE.Box3().setFromObject(content);
    const step = bounds.getSize(new THREE.Vector3()).length() * 0.04;
    for (let iteration = 0; iteration < 40; iteration++) {
      let moved = false;
      const rects = placedNotes.map((note) => note.element.getBoundingClientRect());
      for (let i = 0; i < placedNotes.length; i++) {
        for (let j = i + 1; j < placedNotes.length; j++) {
          const a = rects[i], b = rects[j];
          const overlaps = a.left < b.right + 6 && b.left < a.right + 6 && a.top < b.bottom + 4 && b.top < a.bottom + 4;
          if (!overlaps) continue;
          const lower = a.top > b.top ? placedNotes[i] : placedNotes[j];
          const upper = lower === placedNotes[i] ? placedNotes[j] : placedNotes[i];
          lower.move(-step);
          upper.move(step);
          moved = true;
        }
      }
      if (!moved) break;
      labelRenderer.render(scene, camera);
    }
  }

  const handle = {
    THREE, scene, camera, renderer, controls, content,
    _pendingLabels: pendingLabels,
    _placedNotes: placedNotes,
    add(...objects) { content.add(...objects); return objects[0]; },
    frame,
    /** fn(elapsedSeconds) runs every frame — use it for animations. */
    onFrame(fn) { frameCallbacks.push(fn); return handle; },
    start() {
      placePendingLabels();
      if (!hasFramed) frame();
      separateNotes();
      renderer.setAnimationLoop(render);
      return handle;
    },
  };
  return handle;
}

/**
 * Sticky-note label with a thin leader line to the thing it names.
 * `target` is a mesh/group (anchors at its center) or a point [x, y, z].
 * Leave out `offset` and the note is placed outward from the model automatically
 * (and pushed apart from other notes); give `offset` [x, y, z] to place it yourself.
 */
function label(sceneHandle, target, text, options = {}) {
  const anchor = options.at ? toVector3(options.at) : anchorWorldPoint(target);

  const noteElement = document.createElement("div");
  const color = NOTE_COLORS.includes(options.color) ? options.color : NOTE_COLORS[label.counter++ % NOTE_COLORS.length];
  noteElement.className = `buddy-note ${color}`;
  noteElement.textContent = text;
  const note = new CSS2DObject(noteElement);

  const leaderGeometry = new THREE.BufferGeometry().setFromPoints([anchor, anchor]);
  const leader = new THREE.Line(
    leaderGeometry,
    new THREE.LineBasicMaterial({ color: 0x111111, transparent: true, opacity: 0.85, depthTest: false })
  );
  leader.renderOrder = 10;
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(1, 12, 12),
    new THREE.MeshBasicMaterial({ color: 0x111111, depthTest: false })
  );
  dot.renderOrder = 11;
  dot.position.copy(anchor);
  sceneHandle.scene.add(note, leader, dot);

  function place(notePosition) {
    note.position.copy(notePosition);
    leaderGeometry.setFromPoints([anchor, notePosition]);
    dot.scale.setScalar(anchor.distanceTo(notePosition) * 0.025 || 0.02);
  }
  const placed = {
    element: noteElement,
    move(deltaY) { place(note.position.clone().add(new THREE.Vector3(0, deltaY, 0))); },
  };
  sceneHandle._placedNotes.push(placed);

  if (options.offset) {
    place(anchor.clone().add(toVector3(options.offset)));
  } else {
    place(anchor.clone().add(new THREE.Vector3(0.8, 0.6, 0)));
    sceneHandle._pendingLabels.push({ anchor, place });
  }
  return note;
}
label.counter = 0;

/** Hand-drawn arrow between two DOM elements (2D cards). Redraws on resize. */
function connect(fromElement, toElement, options = {}) {
  let layer = document.querySelector("svg.buddy-arrows");
  if (!layer) {
    layer = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    layer.classList.add("buddy-arrows");
    layer.innerHTML =
      '<defs><marker id="buddy-arrowhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse">' +
      '<path d="M0,0 L10,5 L0,10" fill="none" stroke="#333" stroke-width="1.6"/></marker></defs>';
    document.body.appendChild(layer);
  }
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", options.color || "#333");
  path.setAttribute("stroke-width", options.width || 1.6);
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("marker-end", "url(#buddy-arrowhead)");
  if (options.dashed) path.setAttribute("stroke-dasharray", "6 5");
  layer.appendChild(path);

  function redraw() {
    const from = fromElement.getBoundingClientRect();
    const to = toElement.getBoundingClientRect();
    const isHorizontal = Math.abs(to.left - from.right) >= Math.abs(to.top - from.bottom) || options.horizontal;
    let startX, startY, endX, endY;
    if (isHorizontal) {
      startX = from.right + 12; startY = from.top + from.height / 2;
      endX = to.left - 12; endY = to.top + to.height / 2;
    } else {
      startX = from.left + from.width / 2; startY = from.bottom + 10;
      endX = to.left + to.width / 2; endY = to.top - 10;
    }
    // A slight sag makes it look drawn rather than ruled.
    const middleX = (startX + endX) / 2 + (endY - startY) * 0.04;
    const middleY = (startY + endY) / 2 + Math.abs(endX - startX) * 0.015;
    path.setAttribute("d", `M${startX},${startY} Q${middleX},${middleY} ${endX},${endY}`);
  }
  redraw();
  window.addEventListener("resize", redraw);
  if (document.fonts) document.fonts.ready.then(redraw);
  return path;
}

let mermaidLoading = null;
/** Renders a Mermaid diagram (flowchart, sequence, state, class, er, …) in the hand-drawn look. */
async function mermaid(element, definition) {
  if (!mermaidLoading) {
    mermaidLoading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "buddy://visual/vendor/mermaid.min.js";
      script.onload = () => {
        window.mermaid.initialize({
          startOnLoad: false,
          look: "handDrawn",
          theme: "neutral",
          fontFamily: "BuddyHand, BuddyHandKo, sans-serif",
          themeVariables: { fontSize: "18px" },
          // Natural size (not stretched to the card width); scaled down below if too big.
          flowchart: { nodeSpacing: 40, rankSpacing: 50, padding: 12, useMaxWidth: false },
          sequence: { useMaxWidth: false },
          state: { useMaxWidth: false },
          class: { useMaxWidth: false },
          er: { useMaxWidth: false },
          securityLevel: "strict",
        });
        resolve(window.mermaid);
      };
      script.onerror = () => reject(new Error("could not load mermaid"));
      document.head.appendChild(script);
    });
  }
  const mermaidLibrary = await mermaidLoading;
  const { svg } = await mermaidLibrary.render("buddy-mermaid-" + Math.random().toString(36).slice(2), definition);
  element.innerHTML = svg;
  const svgElement = element.querySelector("svg");
  if (svgElement) {
    svgElement.style.display = "block";
    svgElement.style.margin = "0 auto";
    svgElement.style.maxWidth = "100%";
    svgElement.style.maxHeight = Math.max(240, window.innerHeight - 140) + "px";
    svgElement.style.height = "auto";
  }
  return svgElement;
}

export const kit = { THREE, scene3d, part, arrow, label, connect, mermaid };
export { THREE, scene3d, part, arrow, label, connect, mermaid };
export default kit;
