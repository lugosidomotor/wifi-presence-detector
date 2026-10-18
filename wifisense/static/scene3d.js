// 3D view of the room: nodes, links with their first Fresnel zone, signal particles and the presence map.
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/CSS2DRenderer.js";

const LIME = new THREE.Color("#c9d236");
const CORAL = new THREE.Color("#ff8576");
const LIGHT_SPEED = 0.2998; // m/ns: wavelength = LIGHT_SPEED / GHz
const PARTICLES = 28;

const rimShader = {
  vertexShader: `
    varying vec3 vNormal; varying vec3 vView;
    void main() {
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      vNormal = normalize(normalMatrix * normal);
      vView = normalize(-mv.xyz);
      gl_Position = projectionMatrix * mv;
    }`,
  fragmentShader: `
    uniform vec3 uColor; uniform float uIntensity; uniform float uBase;
    varying vec3 vNormal; varying vec3 vView;
    void main() {
      float rim = pow(1.0 - abs(dot(normalize(vNormal), normalize(vView))), 2.2);
      float alpha = uBase + (0.05 + 0.55 * uIntensity) * rim + 0.1 * uIntensity;
      gl_FragColor = vec4(uColor, alpha);
    }`,
};

function rimMaterial(color, base = 0.015) {
  return new THREE.ShaderMaterial({
    uniforms: { uColor: { value: new THREE.Color(color) }, uIntensity: { value: 0 }, uBase: { value: base } },
    vertexShader: rimShader.vertexShader,
    fragmentShader: rimShader.fragmentShader,
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
  });
}

function label(text, className = "") {
  const element = document.createElement("div");
  element.className = `node-label ${className}`.trim();
  element.textContent = text;
  return new CSS2DObject(element);
}

function dotTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  gradient.addColorStop(0, "rgba(255,255,255,1)");
  gradient.addColorStop(0.35, "rgba(255,255,255,.6)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 64, 64);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

export function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

export class SpaceView {
  constructor(container, layout, colors) {
    this.container = container;
    this.layout = layout;
    this.colors = colors;
    this.room = layout.room;
    this.center = new THREE.Vector3(this.room.width / 2, this.room.height / 3, this.room.depth / 2);
    this.links = new Map();
    this.time = 0;
    this.position = null;
    this.ghostLevel = 0;
    this.truthLevel = 0;

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x000000, 0);
    container.prepend(this.renderer.domElement);
    this.labels = new CSS2DRenderer();
    Object.assign(this.labels.domElement.style, { position: "absolute", inset: "0", pointerEvents: "none" });
    container.appendChild(this.labels.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(42, 1, 0.05, 200);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI * 0.49;
    this.controls.minDistance = 2;
    this.controls.maxDistance = 40;
    this.controls.target.copy(this.center);
    this.viewGoal = null;

    this.scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x2a2018, 1.4));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(4, 8, 3);
    this.scene.add(sun);

    this.dot = dotTexture();
    this.buildRoom();
    this.buildNodes();
    this.buildLinks();
    this.buildPresence();
    this.setView("3d", true);

    this.resize = this.resize.bind(this);
    new ResizeObserver(this.resize).observe(container);
    this.resize();
    this.last = performance.now();
    this.renderer.setAnimationLoop((now) => this.frame(now));
  }

  buildRoom() {
    const { width: w, depth: d, height: h } = this.room;
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(w, d),
      new THREE.MeshStandardMaterial({ color: 0x24303c, roughness: 0.95, transparent: true, opacity: 0.85 }),
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(w / 2, 0, d / 2);
    this.scene.add(floor);

    const grid = [];
    for (let x = 0; x <= w + 1e-6; x += 0.5) grid.push(x, 0.002, 0, x, 0.002, d);
    for (let z = 0; z <= d + 1e-6; z += 0.5) grid.push(0, 0.002, z, w, 0.002, z);
    const gridGeometry = new THREE.BufferGeometry();
    gridGeometry.setAttribute("position", new THREE.Float32BufferAttribute(grid, 3));
    this.scene.add(new THREE.LineSegments(gridGeometry, new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.07 })));

    const box = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d)),
      new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.28 }),
    );
    box.position.set(w / 2, h / 2, d / 2);
    this.scene.add(box);
    // Two faint back walls give the box a sense of inside and outside.
    const wallMaterial = new THREE.MeshBasicMaterial({ color: 0x9fb4c8, transparent: true, opacity: 0.045, side: THREE.DoubleSide, depthWrite: false });
    const back = new THREE.Mesh(new THREE.PlaneGeometry(w, h), wallMaterial);
    back.position.set(w / 2, h / 2, 0);
    const side = new THREE.Mesh(new THREE.PlaneGeometry(d, h), wallMaterial);
    side.rotation.y = Math.PI / 2;
    side.position.set(0, h / 2, d / 2);
    this.scene.add(back, side);

    const size = label(`${w.toLocaleString("hu-HU")} × ${d.toLocaleString("hu-HU")} m`, "truth");
    size.position.set(w / 2, 0, d + 0.25);
    this.scene.add(size);

    // Presence map on the floor: one texel per localisation cell, smoothly filtered.
    this.heatCanvas = null;
    this.heat = new THREE.Mesh(
      new THREE.PlaneGeometry(w, d),
      new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending }),
    );
    this.heat.rotation.x = -Math.PI / 2;
    this.heat.position.set(w / 2, 0.006, d / 2);
    this.scene.add(this.heat);
  }

  buildNodes() {
    this.nodes = new Map();
    for (const node of this.layout.nodes) {
      const [x, y, z] = node.position;
      const group = new THREE.Group();
      group.position.set(x, y, z);
      const ap = node.kind === "ap";
      const body = ap
        ? new THREE.Mesh(new THREE.SphereGeometry(0.09, 32, 16), new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xffffff, emissiveIntensity: 0.6 }))
        : new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.05, 0.11), new THREE.MeshStandardMaterial({ color: 0xd8e0e6, emissive: 0x55606a, roughness: 0.5 }));
      group.add(body);
      const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.dot, color: ap ? 0xffffff : LIME, transparent: true, opacity: ap ? 0.55 : 0.35, depthWrite: false, blending: THREE.AdditiveBlending }));
      glow.scale.setScalar(ap ? 0.6 : 0.4);
      group.add(glow);
      const tag = label(node.name);
      tag.position.set(0, 0.06, 0);
      group.add(tag);
      // Drop line and floor ring: the height of a node is hard to read in perspective otherwise.
      const drop = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, -y, 0)]);
      const dropLine = new THREE.Line(drop, new THREE.LineDashedMaterial({ color: 0xffffff, dashSize: 0.06, gapSize: 0.05, transparent: true, opacity: 0.3 }));
      dropLine.computeLineDistances();
      group.add(dropLine);
      const ring = new THREE.Mesh(new THREE.RingGeometry(0.07, 0.1, 32), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.3, side: THREE.DoubleSide }));
      ring.rotation.x = -Math.PI / 2;
      ring.position.y = -y + 0.004;
      group.add(ring);
      this.scene.add(group);
      this.nodes.set(node.id, new THREE.Vector3(x, y, z));
    }
  }

  buildLinks() {
    this.layout.links.forEach((link, index) => {
      const a = this.nodes.get(link.tx), b = this.nodes.get(link.rx);
      const color = new THREE.Color(this.colors[index % this.colors.length]);
      const length = a.distanceTo(b);
      const radius = Math.max(0.5 * Math.sqrt((LIGHT_SPEED / link.ghz) * length), 0.08);
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([a, b]),
        new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.55 }),
      );
      const material = rimMaterial(color);
      const zone = new THREE.Mesh(new THREE.SphereGeometry(1, 48, 24), material);
      zone.scale.set(length / 2, radius, radius);
      zone.position.copy(a).add(b).multiplyScalar(0.5);
      zone.quaternion.setFromUnitVectors(new THREE.Vector3(1, 0, 0), b.clone().sub(a).normalize());
      const positions = new Float32Array(PARTICLES * 3);
      const pointsGeometry = new THREE.BufferGeometry();
      pointsGeometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const points = new THREE.Points(pointsGeometry, new THREE.PointsMaterial({
        map: this.dot, color, size: 0.09, transparent: true, opacity: 0.85, depthWrite: false, blending: THREE.AdditiveBlending,
      }));
      // Two unit vectors across the link, for particle wobble inside the zone.
      const axis = b.clone().sub(a).normalize();
      const across = new THREE.Vector3().crossVectors(axis, new THREE.Vector3(0, 1, 0));
      if (across.lengthSq() < 1e-6) across.set(1, 0, 0);
      across.normalize();
      const up = new THREE.Vector3().crossVectors(across, axis).normalize();
      this.scene.add(line, zone, points);
      this.links.set(link.id, {
        a, b, color, line, zone, material, points, positions, axis, across, up, radius,
        phases: Array.from({ length: PARTICLES }, (_, i) => i / PARTICLES + Math.random() * 0.02),
        target: 0, level: 0, alarm: false,
      });
    });
  }

  buildPresence() {
    this.ghost = new THREE.Group();
    const material = rimMaterial(CORAL, 0.02);
    const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.22, 1.25, 8, 24), material);
    body.position.y = 0.85;
    this.ghostMaterial = material;
    this.ghostRing = new THREE.Mesh(new THREE.RingGeometry(0.3, 0.38, 48), new THREE.MeshBasicMaterial({ color: CORAL, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false }));
    this.ghostRing.rotation.x = -Math.PI / 2;
    this.ghostRing.position.y = 0.01;
    this.ghostLabel = label("becsült hely", "estimate");
    this.ghostLabel.position.y = 1.85;
    this.ghost.add(body, this.ghostRing, this.ghostLabel);
    this.ghost.visible = false;
    this.scene.add(this.ghost);

    // Simulator ground truth: a dashed outline where the simulated person really is.
    this.truth = new THREE.Group();
    const outline = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.CylinderGeometry(0.22, 0.22, 1.75, 16, 1)),
      new THREE.LineDashedMaterial({ color: 0xffffff, dashSize: 0.07, gapSize: 0.06, transparent: true, opacity: 0.55 }),
    );
    outline.computeLineDistances();
    outline.position.y = 0.875;
    // Under the outline: the estimate's label sits above the ghost, and the two often overlap.
    const truthLabel = label("valós (szimuláció)", "truth");
    truthLabel.position.y = -0.12;
    this.truth.add(outline, truthLabel);
    this.truth.visible = false;
    this.scene.add(this.truth);
    this.cat = new THREE.Mesh(new THREE.SphereGeometry(0.12, 16, 8), new THREE.MeshBasicMaterial({ color: 0xffffff, wireframe: true, transparent: true, opacity: 0.4 }));
    this.cat.scale.set(1.6, 0.8, 0.8);
    this.cat.visible = false;
    this.scene.add(this.cat);
  }

  setView(view, instant = false) {
    const { width: w, depth: d, height: h } = this.room;
    const span = Math.max(w, d);
    const goal = view === "top"
      ? { position: new THREE.Vector3(w / 2, span * 1.9, d / 2 + 0.01), target: new THREE.Vector3(w / 2, 0, d / 2) }
      : { position: new THREE.Vector3(w * 1.15 + span * 0.35, h + span * 0.85, d * 1.25 + span * 0.55), target: this.center.clone() };
    if (instant) {
      this.camera.position.copy(goal.position);
      this.controls.target.copy(goal.target);
      this.controls.update();
    } else {
      this.viewGoal = goal;
    }
  }

  resize() {
    const { clientWidth: width, clientHeight: height } = this.container;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.labels.setSize(width, height);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  update(snapshot, threshold = 1) {
    const alarm = !!snapshot.alarm;
    for (const [id, link] of this.links) {
      const scores = snapshot.links?.[id];
      const score = scores && scores.valid ? Math.max(scores.motion || 0, scores.presence || 0) : 0;
      link.target = Math.min(score / (threshold * 2), 1);
      link.alarm = alarm && score >= threshold;
    }
    this.position = snapshot.position;
    this.updateHeat(snapshot.heat, alarm);
    const sim = snapshot.sim;
    this.truthTarget = sim?.person ?? null;
    this.catTarget = sim?.cat ?? null;
  }

  updateHeat(heat, alarm) {
    const material = this.heat.material;
    if (!heat) {
      this.heatGoal = 0;
      return;
    }
    if (!this.heatCanvas || this.heatCanvas.width !== heat.cols || this.heatCanvas.height !== heat.rows) {
      this.heatCanvas = document.createElement("canvas");
      this.heatCanvas.width = heat.cols;
      this.heatCanvas.height = heat.rows;
      material.map?.dispose();
      material.map = new THREE.CanvasTexture(this.heatCanvas);
      material.map.colorSpace = THREE.SRGBColorSpace;
      material.map.minFilter = material.map.magFilter = THREE.LinearFilter;
      material.needsUpdate = true;
    }
    const ctx = this.heatCanvas.getContext("2d");
    const image = ctx.createImageData(heat.cols, heat.rows);
    const tint = alarm ? [255, 133, 118] : [201, 210, 54];
    for (let row = 0; row < heat.rows; row++) {
      for (let col = 0; col < heat.cols; col++) {
        const v = heat.values[row * heat.cols + col];
        // Canvas row 0 ends up at z = 0 (the flipped texture meets the plane rotated onto the floor): no flip.
        const offset = (row * heat.cols + col) * 4;
        const strength = v * v;
        image.data[offset] = tint[0] * strength;
        image.data[offset + 1] = tint[1] * strength;
        image.data[offset + 2] = tint[2] * strength;
        image.data[offset + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
    material.map.needsUpdate = true;
    this.heatGoal = 0.85;
  }

  frame(now) {
    const dt = Math.min(Math.max((now - this.last) / 1000, 0), 0.1);
    this.last = now;
    this.time += dt;
    const ease = 1 - Math.exp(-dt * 6);
    if (this.viewGoal) {
      this.camera.position.lerp(this.viewGoal.position, ease);
      this.controls.target.lerp(this.viewGoal.target, ease);
      if (this.camera.position.distanceTo(this.viewGoal.position) < 0.02) this.viewGoal = null;
    }
    for (const link of this.links.values()) {
      link.level += (link.target - link.level) * ease;
      const tone = link.color.clone().lerp(link.alarm ? CORAL : LIME, Math.min(link.level * 1.6, 1));
      link.material.uniforms.uColor.value.copy(tone);
      link.material.uniforms.uIntensity.value = link.level;
      link.line.material.color.copy(tone);
      link.points.material.color.copy(tone);
      const speed = 0.12 + link.level * 0.25;
      const wobble = link.radius * (0.08 + link.level * 0.9);
      for (let i = 0; i < PARTICLES; i++) {
        const phase = (link.phases[i] + this.time * speed) % 1;
        const swirl = this.time * (2 + link.level * 7) + i * 2.4;
        // Wobble fades out at both ends: the zone is an ellipsoid pinned to the antennas.
        const envelope = Math.sin(Math.PI * phase) * wobble;
        const p = link.a.clone().lerp(link.b, phase)
          .addScaledVector(link.across, Math.cos(swirl) * envelope)
          .addScaledVector(link.up, Math.sin(swirl * 1.3) * envelope);
        link.positions.set([p.x, p.y, p.z], i * 3);
      }
      link.points.geometry.attributes.position.needsUpdate = true;
    }
    const heatMaterial = this.heat.material;
    heatMaterial.opacity += ((this.heatGoal || 0) - heatMaterial.opacity) * ease;
    this.heat.visible = heatMaterial.opacity > 0.01 && !!heatMaterial.map;

    this.ghostLevel += ((this.position ? 1 : 0) - this.ghostLevel) * ease;
    if (this.position) {
      const goal = new THREE.Vector3(this.position[0], 0, this.position[2]);
      if (!this.ghost.visible) this.ghost.position.copy(goal);
      this.ghost.position.lerp(goal, ease * 0.6);
    }
    this.ghost.visible = this.ghostLevel > 0.02;
    this.ghostMaterial.uniforms.uIntensity.value = 0.5 * this.ghostLevel;
    const pulse = (this.time * 0.8) % 1;
    this.ghostRing.scale.setScalar(1 + pulse * 1.6);
    this.ghostRing.material.opacity = (1 - pulse) * 0.6 * this.ghostLevel;
    this.ghostLabel.element.style.opacity = this.ghostLevel.toFixed(2);

    if (this.truthTarget) {
      const goal = new THREE.Vector3(this.truthTarget[0], 0, this.truthTarget[2]);
      if (!this.truth.visible) this.truth.position.copy(goal);
      this.truth.position.lerp(goal, ease);
    }
    this.truth.visible = !!this.truthTarget;
    if (this.catTarget) this.cat.position.lerp(new THREE.Vector3(this.catTarget[0], 0.12, this.catTarget[2]), this.cat.visible ? ease : 1);
    this.cat.visible = !!this.catTarget;

    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
  }
}
