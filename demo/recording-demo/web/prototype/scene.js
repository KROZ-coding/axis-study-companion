// 液态金属主视觉：手写 WebGL，无外部依赖、无贴图文件。
//
// 结构借鉴 rhine-lock 的场景思路（一条沿无限符号曲线连续流动的截面带、像
// 笔画一样装配出来、解锁时逐段化散），材质换成液态金属：圆角倒角实体 +
// 程序化环境反射 + 菲涅尔冰蓝边光。
//
// 背景与金属共享同一个世界函数 envColor()：背景是相机看向这个世界的结果，
// 金属反射的也是这个世界。所以金属上出现的明暗条纹能对上背景，才像真金属。

const TAU = Math.PI * 2;

const clamp = (x, a, b) => (x < a ? a : x > b ? b : x);

// ── 线性代数小工具 ──────────────────────────────────────────────────────
function perspective(fov, aspect, near, far) {
  const f = 1 / Math.tan(fov / 2);
  const nf = 1 / (near - far);
  return new Float32Array([
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, 2 * far * near * nf, 0,
  ]);
}

function lookAt(eye, target, up) {
  const z = normalize([eye[0] - target[0], eye[1] - target[1], eye[2] - target[2]]);
  const x = normalize(cross(up, z));
  const y = cross(z, x);
  return new Float32Array([
    x[0], y[0], z[0], 0,
    x[1], y[1], z[1], 0,
    x[2], y[2], z[2], 0,
    -dot(x, eye), -dot(y, eye), -dot(z, eye), 1,
  ]);
}

function translation(x, y, z) {
  return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]);
}

function rotationX(angle) {
  const c = Math.cos(angle), s = Math.sin(angle);
  return new Float32Array([1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]);
}

function rotationY(angle) {
  const c = Math.cos(angle), s = Math.sin(angle);
  return new Float32Array([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]);
}

function rotationZ(angle) {
  const c = Math.cos(angle), s = Math.sin(angle);
  return new Float32Array([c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
}

function multiply(a, b) {
  const out = new Float32Array(16);
  for (let col = 0; col < 4; col += 1) {
    for (let row = 0; row < 4; row += 1) {
      out[col * 4 + row] =
        a[row] * b[col * 4] +
        a[4 + row] * b[col * 4 + 1] +
        a[8 + row] * b[col * 4 + 2] +
        a[12 + row] * b[col * 4 + 3];
    }
  }
  return out;
}

const cross = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
function normalize(v) {
  const l = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
}
const mix3 = (a, b, t) => [
  a[0] + (b[0] - a[0]) * t,
  a[1] + (b[1] - a[1]) * t,
  a[2] + (b[2] - a[2]) * t,
];

// ── 几何：沿无限符号曲线扫掠的圆角截面 ─────────────────────────────────
// 截面是圆角矩形，倒角很大，所以整条带子光滑、没有硬棱——"液态"来自
// 大倒角 + 镜面反射，而不是靠多边形面片的明暗拼贴。
const SHAPE = {
  segments: 520,
  ring: 28,
  corner: 0.30,   // 0 = 方棱，1 = 完全椭圆；0.3 保留扁带子的侧壁，仍无硬棱
  radius: 1.0,
  aspect: 0.92,   // 曲线在竖直方向压扁
  depth: 0.52,    // 深度方向张开，带子会从自己上方越过
  halfWidth: 0.062,
  height: 0.105,
  twist: 1.28,
};

function curveAt(t, shape) {
  const s = Math.sin(t);
  const c = Math.cos(t);
  return [shape.radius * c, shape.radius * shape.aspect * s * c, shape.depth * s];
}

function curveTangent(t, shape) {
  const h = 0.0015;
  const a = curveAt(t - h, shape);
  const b = curveAt(t + h, shape);
  return normalize([b[0] - a[0], b[1] - a[1], b[2] - a[2]]);
}

// 截面：四个圆角的闭合轮廓，点数 = perCorner * 4
function sectionProfile(corner, perCorner) {
  const inner = 1 - corner;
  const corners = [
    [inner, inner, 0],
    [-inner, inner, Math.PI / 2],
    [-inner, -inner, Math.PI],
    [inner, -inner, Math.PI * 1.5],
  ];
  const points = [];
  for (const [cx, cy, base] of corners) {
    for (let i = 0; i < perCorner; i += 1) {
      const a = base + (i / perCorner) * (Math.PI / 2);
      points.push([cx + Math.cos(a) * corner, cy + Math.sin(a) * corner]);
    }
  }
  return points;
}

function buildBand(shape) {
  const { segments: SEG, ring: RING, halfWidth, height } = shape;
  const profile = sectionProfile(shape.corner, Math.max(2, Math.round(RING / 4)));
  const P = profile.length;
  const GROUPS = 26;
  const surface = [];
  const surfaceNormals = [];
  const point = (s, r) => surface[((s + SEG) % SEG) * P + ((r + P) % P)];

  for (let s = 0; s < SEG; s += 1) {
    const t = (s / SEG) * TAU;
    const center = curveAt(t, shape);
    const T = curveTangent(t, shape);
    const N0 = normalize([-T[2] * T[0], -T[2] * T[1], 1 - T[2] * T[2]]);
    const B0 = cross(T, N0);
    // 周期扭转：首尾截面和导数都闭合，保留原来的无限符号轮廓。
    const phi = t + (shape.twist - 1) * Math.sin(t);
    const W = N0.map((n, i) => Math.cos(phi) * n + Math.sin(phi) * B0[i]);
    const H = cross(T, W);
    for (const [px, py] of profile) {
      surface.push(center.map((c, i) => c + W[i] * px * halfWidth + H[i] * py * height));
    }
  }
  // 法线在完整曲面上求，再复制到分段边界，装配后没有分段明暗接缝。
  for (let s = 0; s < SEG; s += 1) {
    for (let r = 0; r < P; r += 1) {
      const along = point(s + 1, r).map((v, i) => v - point(s - 1, r)[i]);
      const around = point(s, r + 1).map((v, i) => v - point(s, r - 1)[i]);
      surfaceNormals.push(normalize(cross(around, along)));
    }
  }

  const positions = [], normals = [], params = [], pieces = [], scatters = [], indices = [];
  const ringSeams = [], groupSeams = [];
  const hash = (n) => { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };
  function vertex(position, normal, param, piece, scatter) {
    const index = positions.length / 3;
    positions.push(...position);
    normals.push(...normal);
    params.push(...param);
    pieces.push(...piece);
    scatters.push(...scatter);
    return index;
  }

  for (let g = 0; g < GROUPS; g += 1) {
    const first = Math.round(g * SEG / GROUPS);
    const last = Math.round((g + 1) * SEG / GROUPS);
    const center = curveAt((first + last) * 0.5 / SEG * TAU, shape);
    const seed = hash(g + 1);
    const radial = normalize([center[0], center[1] * 1.5, 0]);
    const spread = 0.22 + seed * 0.13;
    let radius = 0;
    for (let s = first; s <= last; s += 1) {
      for (let r = 0; r < P; r += 1) {
        radius = Math.max(radius, Math.hypot(...point(s, r).map((v, i) => v - center[i])));
      }
    }
    const piece = [...center, seed];
    const scatter = [
      radial[0] * spread + (hash(g + 31) - 0.5) * 0.055,
      radial[1] * spread * 0.78 + (hash(g + 73) - 0.5) * 0.055,
      // 大于刚体旋转的最大前向位移，任意拖拽朝向下都不会靠近相机。
      -radius * (0.40 + seed * 0.60) - 0.06 - hash(g + 91) * 0.06,
      g / GROUPS * 0.12 + seed * 0.10,
    ];
    const base = positions.length / 3;
    for (let s = first; s <= last; s += 1) {
      const ringStart = positions.length / 3;
      for (let r = 0; r <= P; r += 1) {
        vertex(point(s, r), surfaceNormals[(s % SEG) * P + (r % P)], [s / SEG, r / P, 0], piece, scatter);
      }
      ringSeams.push([ringStart, ringStart + P]);
    }
    if (g > 0) groupSeams.push([base - (2 * (P + 2)) - (P + 1), base]);
    for (let s = 0; s < last - first; s += 1) {
      for (let r = 0; r < P; r += 1) {
        const a = base + s * (P + 1) + r;
        const b = a + 1, d = a + P + 1, c = d + 1;
        indices.push(a, b, c, a, c, d);
      }
    }
    // 封口单独使用端面法线；散开后才显示，完整轮廓不叠出接缝。
    for (const [s, sign] of [[first, -1], [last, 1]]) {
      const t = (s % SEG) / SEG * TAU;
      const normal = curveTangent(t, shape).map((v) => v * sign);
      const cap = vertex(curveAt(t, shape), normal, [s / SEG, 0.5, 1], piece, scatter);
      for (let r = 0; r <= P; r += 1) {
        vertex(point(s, r), normal, [s / SEG, r / P, 1], piece, scatter);
        if (r < P) {
          const a = cap + 1 + r, b = a + 1;
          indices.push(cap, sign > 0 ? a : b, sign > 0 ? b : a);
        }
      }
    }
  }
  // 最后一段与第一段共用同一组位置/法线，参数仍保留 1 / 0 的周期边界。
  const lastSide = positions.length / 3 - 2 * (P + 2) - (P + 1);
  groupSeams.push([lastSide, 0]);
  function seamError(pairs, values, width, count = 1) {
    let max = 0;
    for (const [a, b] of pairs) {
      for (let r = 0; r < count; r += 1) {
        for (let k = 0; k < width; k += 1) {
          max = Math.max(max, Math.abs(values[(a + r) * width + k] - values[(b + r) * width + k]));
        }
      }
    }
    return max;
  }
  const stats = Object.freeze({
    vertices: positions.length / 3,
    triangles: indices.length / 3,
    segments: SEG,
    ring: P,
    groups: GROUPS,
    ringSeam: Object.freeze({
      count: ringSeams.length,
      duplicated: true,
      maxPositionDelta: seamError(ringSeams, positions, 3),
      maxNormalDelta: seamError(ringSeams, normals, 3),
    }),
    groupSeam: Object.freeze({
      count: groupSeams.length,
      maxPositionDelta: seamError(groupSeams, positions, 3, P + 1),
      maxNormalDelta: seamError(groupSeams, normals, 3, P + 1),
    }),
    loopSeam: Object.freeze({
      vertices: P + 1,
      maxPositionDelta: seamError([[lastSide, 0]], positions, 3, P + 1),
      maxNormalDelta: seamError([[lastSide, 0]], normals, 3, P + 1),
    }),
  });
  return {
    position: new Float32Array(positions),
    normal: new Float32Array(normals),
    param: new Float32Array(params),
    piece: new Float32Array(pieces),
    scatter: new Float32Array(scatters),
    index: new Uint16Array(indices),
    count: indices.length,
    stats,
  };
}

// ── GLSL ──────────────────────────────────────────────────────────────
// 世界：冰蓝天空 + 左上柔光灯板 + 深色地台 + 一道斜向灯带。
// 地台很暗，镜面金属才有那种明暗交替的条带，否则一片白就糊在背景里了。
const ENV_GLSL = `
float e_hash(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

// 金属的反射环境：摄影棚。
// 铬的辨识度来自"硬边"——环境里必须有突然亮/突然暗的边界，反射出来才是
// 清晰的明暗条带。全用 smoothstep 的柔和渐变，无论调多亮都只会糊成一片。
vec3 envColor(vec3 d) {
  d = normalize(d);
  float up = d.y;
  float lon = atan(d.z, d.x);      // -pi..pi，绕竖直轴

  // 底色：上方亮、下方暗，这是整体基调。
  // 但不能压太狠——下半圈若接近全黑，金属会像一块黑塑料而不是铬。
  vec3 col = mix(vec3(0.26, 0.29, 0.32), vec3(0.94, 0.96, 0.98),
                 smoothstep(-0.10, 0.62, up));

  // 竖条纹灯箱：用 sin 的硬边切出亮/暗竖条。
  // 这是全场景最关键的结构——相机看向物体时反射方向主要绕竖直轴游走，
  // 只有这一圈存在硬边明暗，表面才会出现一条亮一条暗的金属条带。
  float bars = sin(lon * 5.0) * 0.6 + sin(lon * 9.0 + 1.1) * 0.4;
  float barMask = smoothstep(0.10, 0.42, bars) - smoothstep(0.58, 0.90, bars);
  col = mix(col * 0.42, col * 1.28, barMask);

  // 一横条深色带：把上下两半硬性分开，接缝处形成一条高对比的明暗界线
  float band = step(0.02, up) * step(up, 0.24);
  col = mix(col, col * 0.30, band);

  // 大灯板：左上前方一块明显的亮区，边界是硬的
  float panel = step(0.40, dot(d, normalize(vec3(-0.55, 0.62, 0.56))));
  col = mix(col, vec3(1.0, 0.99, 0.96), panel * 0.85);

  // 细灯管：一条极窄的亮线，转动时在表面游走
  float tube = step(0.9900, dot(d, normalize(vec3(0.30, 0.80, -0.52))));
  col = mix(col, vec3(1.0), tube);

  // 右侧暗区
  float wall = step(0.48, d.x) * step(-0.25, up);
  col = mix(col, col * 0.34, wall);

  // 地平线以下：略暗的地台，但保留反光，不能死黑
  col = mix(col, vec3(0.175, 0.200, 0.230), smoothstep(0.0, -0.34, up) * 0.90);

  return col;
}
`;

const COMMON_FS_HEAD = `
precision highp float;
${ENV_GLSL}
`;

const BACKGROUND_VS = `
attribute vec2 aPos;
varying vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

// 页面背景：冰蓝极光。与金属的反射环境分开——网页底色该是通透的浅色，
// 金属反射的却是摄影棚里那种高对比的环境，两者数值不同才成立。
const BACKGROUND_FS = `
precision highp float;
varying vec2 vUv;
uniform vec2 uRes;
uniform float uTime;
uniform float uReveal;
uniform float uVerify;
uniform float uDeny;
uniform float uStandby;

float b_hash(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}
float b_noise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(b_hash(i), b_hash(i + vec2(1.0, 0.0)), u.x),
             mix(b_hash(i + vec2(0.0, 1.0)), b_hash(i + vec2(1.0, 1.0)), u.x), u.y);
}
float b_fbm(vec2 p) {
  float v = 0.0, a = 0.5;
  for (int i = 0; i < 5; i++) { v += a * b_noise(p); p = p * 2.03 + 11.7; a *= 0.5; }
  return v;
}

void main() {
  vec2 uv = vUv;
  float aspect = uRes.x / max(uRes.y, 1.0);
  vec2 p = (uv - vec2(0.5)) * vec2(aspect, 1.0);

  // 底色：上方留白，越往下越是青蓝——页面要通透，但得有颜色
  vec3 top = vec3(0.938, 0.976, 0.992);
  vec3 bottom = vec3(0.520, 0.735, 0.868);
  vec3 col = mix(bottom, top, smoothstep(-0.50, 0.26, p.y));

  // 两层缓慢流动的雾
  float n1 = b_fbm(p * 1.9 + vec2(uTime * 0.011, -uTime * 0.008));
  float n2 = b_fbm(p * 3.6 - vec2(uTime * 0.018, uTime * 0.012));
  col = mix(col, mix(bottom, top, 0.88), (n1 * 0.55 + n2 * 0.30) * 0.38);

  // 左上冷光、右下暖灰：撑开空间感，也呼应金属反射里的光源。
  // 两处都要很淡，否则会在底上留下突兀的暗斑。
  float key = exp(-dot(p - vec2(-0.62, 0.44), p - vec2(-0.62, 0.44)) * 2.4);
  col = mix(col, col * vec3(1.03, 1.04, 1.03), key * 0.55);
  float warm = exp(-dot(p - vec2(0.76, -0.46), p - vec2(0.76, -0.46)) * 3.2);
  col = mix(col, col * vec3(1.05, 1.02, 0.98), warm * 0.40);

  // 状态色
  col = mix(col, col * vec3(1.01, 1.03, 1.05) + vec3(0.012, 0.030, 0.040), uVerify * 0.55);
  col = mix(col, vec3(0.205, 0.122, 0.088), uDeny * 0.66);
  col = mix(col, col * 0.90, uStandby * 0.55);
  col = mix(col, col * 1.015 + 0.005, uReveal * 0.6);

  // 细颗粒 + 暗角
  col += (b_hash(uv * uRes + fract(floor(uTime * 20.0) * 0.137) * 977.0) - 0.5) * 0.015;
  float vig = smoothstep(1.40, 0.28, length(p * vec2(0.80, 1.0)));
  col *= mix(1.0, vig, 0.34);

  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}`;

const BAND_VS = `
precision highp float;
attribute vec3 aPos;
attribute vec3 aNrm;
attribute vec3 aParam;
attribute vec4 aPiece;
attribute vec4 aScatter;
uniform mat4 uProj;
uniform mat4 uView;
uniform mat4 uModel;
uniform mat3 uScatterBasis;
uniform float uTime;
uniform float uAssemble;
uniform float uDissolve;
uniform float uWobble;
varying vec3 vWorld;
varying vec3 vNrm;
varying vec2 vParam;
varying float vTip;
varying float vSeed;
varying float vCap;
varying float vOpacity;

vec3 rotatePiece(vec3 v, vec3 axis, float angle) {
  float c = cos(angle), s = sin(angle);
  return v * c + cross(axis, v) * s + axis * dot(axis, v) * (1.0 - c);
}

void main() {
  vec3 p = aPos;
  vec3 n = aNrm;
  float param = aParam.x;
  float seed = aPiece.w;
  // 呼吸在分离时收敛；每个玻璃段随后只做刚体运动。
  vec3 radial = normalize(p + vec3(0.0, 0.0, 0.001));
  float breath = sin(param * 6.2831853 * 2.0 + uTime * 0.62) * 0.6
               + sin(param * 6.2831853 * 5.0 - uTime * 0.41 + 1.7) * 0.4;
  p += radial * breath * uWobble * (1.0 - smoothstep(0.0, 0.22, uDissolve)) * 0.022;

  // 装配笔尖保留；分段边界只复制顶点，不再连接会被拉长的三角形。
  float win = 0.085;
  float local = clamp((uAssemble * (1.0 + win) - param) / win, 0.0, 1.0);
  float settle = 1.0 - pow(1.0 - local, 3.0);
  float grow = local > 0.0
    ? 1.0 + 2.70158 * pow(local - 1.0, 3.0) + 1.70158 * pow(local - 1.0, 2.0)
    : 0.0;
  p = p * grow + normalize(p + vec3(0.0, 0.0, 40.0)) * (1.0 - settle) * 0.55;

  // 整段剥离、错开旋转、缓慢外漂，再淡出；最迟一段也在进度 1 前归零。
  float peel = clamp((uDissolve - aScatter.w) / (1.0 - aScatter.w), 0.0, 1.0);
  float spread = peel * peel * (3.0 - 2.0 * peel);
  vec3 axis = normalize(vec3(0.35 + seed, 0.8 - seed * 1.4, 0.45));
  float angle = spread * (0.40 + seed * 0.60) * (seed < 0.5 ? -1.0 : 1.0);
  p = aPiece.xyz + rotatePiece(p - aPiece.xyz, axis, angle);
  n = rotatePiece(n, axis, angle);
  // 位移转换自相机平面；后退量还覆盖局部旋转，不会因拖拽朝向而冲向镜头。
  p += uScatterBasis * aScatter.xyz * spread;

  vec4 world = uModel * vec4(p, 1.0);
  vWorld = world.xyz;
  vNrm = mat3(uModel) * n;
  vParam = aParam.xy;
  float dt = param - uAssemble * (1.0 + win) + win * 0.5;
  vTip = exp(-dt * dt * 3200.0) * (1.0 - smoothstep(0.9, 1.0, uAssemble));
  vSeed = 0.5 + 0.5 * sin(param * 18.8495559);
  vCap = aParam.z;
  vOpacity = (1.0 - smoothstep(0.45, 1.0, peel)) * smoothstep(0.0, 0.10, local)
           * mix(1.0, smoothstep(0.0, 0.12, peel), aParam.z);
  gl_Position = uProj * uView * world;
}`;

const BAND_FS = `${COMMON_FS_HEAD}
varying vec3 vWorld;
varying vec3 vNrm;
varying vec2 vParam;
varying float vTip;
varying float vSeed;
varying float vCap;
varying float vOpacity;
uniform vec3 uEye;
uniform float uTime;
uniform float uAssemble;
uniform float uVerify;
uniform float uDeny;
uniform float uReveal;
uniform sampler2D uBgTex;
uniform vec2 uRes;

void main() {
  if (vOpacity <= 0.001) discard;
  vec3 N = normalize(vNrm);
  vec3 V = normalize(uEye - vWorld);
  if (!gl_FrontFacing) N = -N;

  // 细纹沿两个周期闭合，复制的截面边界保持同一法线。
  float ringAngle = vParam.y * 6.2831853;
  float ripple = sin(vParam.x * 194.7787445 + uTime * 1.35)
               + sin(vParam.x * 56.5486678 - uTime * 0.88 + ringAngle) * 0.7;
  vec3 jitter = vec3(cos(ringAngle) * ripple, sin(ringAngle) * ripple, 0.0);
  N = normalize(N + jitter * 0.010 * (0.4 + vSeed * 0.8) * (1.0 - vCap));

  float facing = clamp(dot(N, V), 0.0, 1.0);

  // 流体玻璃：不是铁黑镜面，而是一块半透明的冰蓝玻璃。
  // 做法 = 稀薄的环境反射 + 大量透出的背景色 + 亮边（菲涅尔）。
  vec3 R = reflect(-V, N);
  vec3 reflectCol = envColor(R);

  float fres = pow(1.0 - facing, 4.0);
  float rim = pow(1.0 - facing, 1.5);

  // 玻璃体：偏冰蓝、通透。反射只占三成，剩下是"透出来的冷色"。
  vec3 glass = mix(vec3(0.78, 0.90, 0.97), reflectCol * 0.72 + 0.26, 0.34);
  // 体积感：朝上的面更亮，朝下的面稍深，但都在浅色范围内
  glass *= 0.94 + 0.10 * (N.y * 0.5 + 0.5);

  // 屏幕空间折射：按法线偏移去采样背景。
  // 这是玻璃"存在"的核心——背后的画面真的被玻璃扭曲了，
  // 只加一层半透明色是出不来这种感觉的。
  vec2 uv = gl_FragCoord.xy / uRes;
  vec2 offset = (N.xy) * 0.055 * (0.45 + fres);
  vec2 uvR = clamp(uv + offset * 1.02, vec2(0.001), vec2(0.999));
  vec2 uvG = clamp(uv + offset, vec2(0.001), vec2(0.999));
  vec2 uvB = clamp(uv + offset * 0.98, vec2(0.001), vec2(0.999));
  // 三通道各偏一点：玻璃边缘那圈隐约的彩色分离
  vec3 refr = vec3(
    texture2D(uBgTex, uvR).r,
    texture2D(uBgTex, uvG).g,
    texture2D(uBgTex, uvB).b
  );
  glass = mix(glass, refr, mix(0.30, 0.62, 1.0 - facing));

  // 内部折射色散：边缘泛一点点青，中间偏白，玻璃才有厚度
  vec3 tint = vec3(0.55, 0.82, 0.95);
  glass = mix(glass, glass * tint * 1.10, rim * 0.35);

  // 两道柔和的亮带：像光从玻璃上滑过
  vec3 H1 = normalize(normalize(vec3(-0.52, 0.78, 0.44)) + V);
  vec3 H2 = normalize(normalize(vec3(0.62, -0.18, 0.52)) + V);
  float s1 = pow(max(dot(N, H1), 0.0), 160.0);
  float s2 = pow(max(dot(N, H2), 0.0), 70.0);
  glass += vec3(1.0) * s1 * 0.70;
  glass += vec3(0.92, 0.97, 1.0) * s2 * 0.26;

  // 边缘高光：玻璃最标志性的那圈亮边
  glass += vec3(0.72, 0.93, 1.0) * fres * 1.05;
  // 极窄的镜面边线，让轮廓清脆
  float edge = smoothstep(0.86, 1.0, rim);
  glass += vec3(1.0) * edge * 0.22;

  // 装配笔尖
  glass += vec3(1.0, 0.99, 0.96) * vTip * (1.0 - uAssemble) * 1.4;
  // 核对时扫过的青线
  glass += vec3(0.55, 0.85, 1.0) * uVerify * rim * 0.7
         * step(0.62, fract(vParam.x * 13.0 - uTime * 1.05));

  // 拒绝：偏暖，但仍保持通透
  glass = mix(glass, glass * vec3(1.18, 0.74, 0.58), uDeny * 0.80);

  glass *= mix(0.86, 1.0, clamp(uReveal * 1.6, 0.0, 1.0));

  // 半透明：正面透得多，边缘几乎实心
  float alpha = clamp(mix(0.30, 0.80, fres), 0.0, 1.0) * vOpacity;
  gl_FragColor = vec4(clamp(glass, 0.0, 1.0), alpha);
}`;

function compile(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.warn("液态金属着色器编译失败：", gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

function link(gl, vsSource, fsSource) {
  const vs = compile(gl, gl.VERTEX_SHADER, vsSource);
  const fs = compile(gl, gl.FRAGMENT_SHADER, fsSource);
  if (!vs || !fs) {
    if (vs) gl.deleteShader(vs);
    if (fs) gl.deleteShader(fs);
    return null;
  }
  const program = gl.createProgram();
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.warn("液态金属程序链接失败：", gl.getProgramInfoLog(program));
    gl.deleteProgram(program);
    return null;
  }
  return program;
}

// ── 场景 ──────────────────────────────────────────────────────────────
export function createScene(canvas, options = {}) {
  const gl = options.context || canvas.getContext("webgl", {
    alpha: false,
    antialias: true,
    depth: true,
    premultipliedAlpha: false,
    powerPreference: "high-performance",
  });
  if (!gl) return null;

  const band = buildBand(SHAPE);
  const doc = canvas.ownerDocument || document;
  const win = doc.defaultView || window;
  const unit = (v) => clamp(Number(v) || 0, 0, 1);
  const hidden = () => Boolean(doc.hidden);
  const clock = () => performance.now();
  let reducedOverride = null;
  const reduce = () => reducedOverride === null
    ? Boolean(typeof options.reduce === "function" ? options.reduce() : options.reduce)
    : reducedOverride;
  let resources = null;
  let transition = null;
  // 完成帧绘制后，还要经过下一次 rAF，父层才可以遮住或移走画布。
  let painted = [];
  const s = {
    time: 0, yaw: 0.40, pitch: 0.17, baseYaw: 0.40, basePitch: 0.17,
    spin: 0, spinSpeed: 0.14, reveal: 0, pull: 0, assemble: 0, dissolve: 0,
    verify: 0, deny: 0, standby: 0, running: false, raf: 0, last: 0,
    contextLost: false, destroyed: false,
  };

  function releaseResources() {
    if (!resources) return;
    for (const buffer of Object.values(resources.buffers)) gl.deleteBuffer(buffer);
    if (resources.bgTex) gl.deleteTexture(resources.bgTex);
    if (resources.bgFbo) gl.deleteFramebuffer(resources.bgFbo);
    if (resources.bgProgram) gl.deleteProgram(resources.bgProgram);
    if (resources.bandProgram) gl.deleteProgram(resources.bandProgram);
    resources = null;
  }

  function initResources() {
    resources = {
      bgProgram: link(gl, BACKGROUND_VS, BACKGROUND_FS),
      bandProgram: link(gl, BAND_VS, BAND_FS),
      buffers: {}, bgTex: null, bgFbo: null, bgTexW: 0, bgTexH: 0,
    };
    const r = resources;
    if (!r.bgProgram || !r.bandProgram) { releaseResources(); return false; }
    function buffer(name, data, target = gl.ARRAY_BUFFER) {
      r.buffers[name] = gl.createBuffer();
      gl.bindBuffer(target, r.buffers[name]);
      gl.bufferData(target, data, gl.STATIC_DRAW);
    }
    buffer("quad", new Float32Array([-1, -1, 3, -1, -1, 3]));
    buffer("position", band.position);
    buffer("normal", band.normal);
    buffer("param", band.param);
    buffer("piece", band.piece);
    buffer("scatter", band.scatter);
    buffer("index", band.index, gl.ELEMENT_ARRAY_BUFFER);
    r.bgTex = gl.createTexture();
    r.bgFbo = gl.createFramebuffer();
    const bg = (name) => gl.getUniformLocation(r.bgProgram, name);
    const mesh = (name) => gl.getUniformLocation(r.bandProgram, name);
    r.u = {
      bgPos: gl.getAttribLocation(r.bgProgram, "aPos"),
      bgRes: bg("uRes"), bgTime: bg("uTime"), bgReveal: bg("uReveal"),
      bgVerify: bg("uVerify"), bgDeny: bg("uDeny"), bgStandby: bg("uStandby"),
      pos: gl.getAttribLocation(r.bandProgram, "aPos"),
      nrm: gl.getAttribLocation(r.bandProgram, "aNrm"),
      prm: gl.getAttribLocation(r.bandProgram, "aParam"),
      piece: gl.getAttribLocation(r.bandProgram, "aPiece"),
      scatter: gl.getAttribLocation(r.bandProgram, "aScatter"),
      proj: mesh("uProj"), view: mesh("uView"), model: mesh("uModel"),
      scatterBasis: mesh("uScatterBasis"), eye: mesh("uEye"), time: mesh("uTime"),
      assemble: mesh("uAssemble"), dissolve: mesh("uDissolve"),
      verify: mesh("uVerify"), deny: mesh("uDeny"), reveal: mesh("uReveal"),
      bgTex: mesh("uBgTex"), res: mesh("uRes"), wobble: mesh("uWobble"),
    };
    return true;
  }
  if (!initResources()) return null;
  // 供入口网格检查使用，数值来自真实重复顶点，而不是预设为零的标记。
  canvas.__sceneStats = band.stats;
  win.__sceneStats = band.stats;

  function ensureBgTarget(w, h) {
    const r = resources;
    if (r.bgTexW === w && r.bgTexH === h) return;
    gl.bindTexture(gl.TEXTURE_2D, r.bgTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindFramebuffer(gl.FRAMEBUFFER, r.bgFbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, r.bgTex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    r.bgTexW = w;
    r.bgTexH = h;
  }

  function render() {
    if (s.destroyed || s.contextLost || !resources || hidden()) return false;
    if (gl.isContextLost && gl.isContextLost()) return false;
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(win.devicePixelRatio || 1, 2);
    const W = Math.max(1, Math.floor((rect.width || win.innerWidth) * dpr));
    const H = Math.max(1, Math.floor((rect.height || win.innerHeight) * dpr));
    if (canvas.width !== W) canvas.width = W;
    if (canvas.height !== H) canvas.height = H;
    const r = resources, u = r.u;
    const still = reduce();
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, W, H);
    gl.depthMask(true);
    gl.clearColor(0, 0, 0, 1);
    gl.clearDepth(1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const aspect = W / Math.max(H, 1);

    // 保留原有完整玻璃的构图；外漂在相机平面内完成，深度只会后退。
    const narrow = aspect < 0.85;
    const fov = narrow ? 0.78 : 0.92;
    const tan = Math.tan(fov / 2);
    const dist = Math.max(1.46 / tan, 1.46 / (tan * aspect)) * (narrow ? 0.92 : 1.06);
    const eye = [0, narrow ? 0.10 : 0.16, dist];
    const target = narrow ? [0, -0.42, 0] : [0, 0, 0];
    const proj = perspective(fov, aspect, 0.1, 60);
    const view = lookAt(eye, target, [0, 1, 0]);

    // 背景先画入折射纹理，再铺回屏幕；先解绑采样纹理，避免反馈环。
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, null);
    ensureBgTarget(W, H);
    gl.bindTexture(gl.TEXTURE_2D, null);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);
    gl.useProgram(r.bgProgram);
    gl.bindBuffer(gl.ARRAY_BUFFER, r.buffers.quad);
    gl.enableVertexAttribArray(u.bgPos);
    gl.vertexAttribPointer(u.bgPos, 2, gl.FLOAT, false, 0, 0);
    gl.uniform2f(u.bgRes, W, H);
    gl.uniform1f(u.bgTime, still ? 0 : s.time);
    gl.uniform1f(u.bgReveal, s.reveal);
    gl.uniform1f(u.bgVerify, s.verify);
    gl.uniform1f(u.bgDeny, s.deny);
    gl.uniform1f(u.bgStandby, s.standby);
    gl.bindFramebuffer(gl.FRAMEBUFFER, r.bgFbo);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.bindTexture(gl.TEXTURE_2D, r.bgTex);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    gl.enable(gl.DEPTH_TEST);
    gl.depthMask(false);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.disable(gl.CULL_FACE);
    gl.useProgram(r.bandProgram);
    for (const [name, location, size] of [
      ["position", u.pos, 3], ["normal", u.nrm, 3], ["param", u.prm, 3],
      ["piece", u.piece, 4], ["scatter", u.scatter, 4],
    ]) {
      gl.bindBuffer(gl.ARRAY_BUFFER, r.buffers[name]);
      gl.enableVertexAttribArray(location);
      gl.vertexAttribPointer(location, size, gl.FLOAT, false, 0, 0);
    }
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, r.buffers.index);

    const shiftX = narrow ? (-0.02 - s.pull * 0.10) : (-0.06 - s.pull * 0.46);
    const lift = (narrow ? 0.06 : 0.62) + s.pull * (narrow ? 0.02 : 0.06);
    const rotation = multiply(rotationY(s.yaw + s.spin), multiply(rotationX(s.pitch), rotationZ(-0.07)));
    const model = multiply(translation(shiftX, lift, 0), rotation);
    const viewRotation = multiply(view, rotation);
    // (view * modelRotation)^T 把相机 XY / 后退 Z 位移变回模型空间。
    const scatterBasis = new Float32Array([
      viewRotation[0], viewRotation[4], viewRotation[8],
      viewRotation[1], viewRotation[5], viewRotation[9],
      viewRotation[2], viewRotation[6], viewRotation[10],
    ]);
    gl.uniformMatrix4fv(u.proj, false, proj);
    gl.uniformMatrix4fv(u.view, false, view);
    gl.uniformMatrix4fv(u.model, false, model);
    gl.uniformMatrix3fv(u.scatterBasis, false, scatterBasis);
    gl.uniform3fv(u.eye, eye);
    gl.uniform1f(u.time, still ? 0 : s.time);
    gl.uniform1f(u.assemble, s.assemble);
    gl.uniform1f(u.dissolve, s.dissolve);
    gl.uniform1f(u.verify, s.verify);
    gl.uniform1f(u.deny, s.deny);
    gl.uniform1f(u.reveal, s.reveal);
    gl.uniform1f(u.wobble, still ? 0 : 1);
    gl.uniform1i(u.bgTex, 0);
    gl.uniform2f(u.res, W, H);
    gl.drawElements(gl.TRIANGLES, band.count, gl.UNSIGNED_SHORT, 0);
    gl.depthMask(true);
    return true;
  }

  function resolve(task) {
    if (!task || task.settled) return;
    task.settled = true;
    task.resolve();
  }
  function cancelTransitions() {
    const task = transition;
    transition = null;
    resolve(task);
    for (const pending of painted) resolve(pending);
    painted = [];
  }
  function settleEndpoint(draw = true) {
    const task = transition;
    transition = null;
    if (task) s[task.channel] = task.to;
    if (draw) render();
    resolve(task);
    for (const pending of painted) resolve(pending);
    painted = [];
  }
  function cancelFrame() {
    if (s.raf) cancelAnimationFrame(s.raf);
    s.raf = 0;
    s.last = 0;
  }
  function schedule() {
    if (s.running && !s.destroyed && !s.contextLost && resources && !hidden() && !s.raf) {
      s.raf = requestAnimationFrame(frame);
    }
  }
  function frame(now) {
    s.raf = 0;
    if (!s.running || s.destroyed || s.contextLost || hidden()) return;
    const previousPaint = painted;
    painted = [];
    const wasTransitioning = Boolean(transition || previousPaint.length);
    const dt = s.last ? clamp((now - s.last) / 1000, 0, 0.05) : 0;
    s.last = now;
    const still = reduce();
    let finished = null;
    if (transition) {
      const task = transition;
      // 真实毫秒时钟与背景动画的限幅 dt 分开，低帧率也按约定时间完成。
      const x = still ? 1 : clamp((now - task.t0) / task.duration, 0, 1);
      const eased = x * x * (3 - 2 * x);
      s[task.channel] = task.from + (task.to - task.from) * eased;
      if (x >= 1) {
        s[task.channel] = task.to;
        transition = null;
        finished = task;
      }
    }
    // 过渡期间保持朝向稳定，重组终点严格回到规范姿态。
    if (!still && !wasTransitioning) {
      s.time += dt;
      s.spin += dt * s.spinSpeed;
      s.yaw = s.baseYaw + Math.sin(s.time * 0.085) * 0.26;
      s.pitch = s.basePitch + Math.sin(s.time * 0.052) * 0.08;
    }
    const drew = render();
    for (const task of previousPaint) resolve(task);
    if (finished) {
      if (drew && !still) painted.push(finished);
      else resolve(finished);
    }
    schedule();
  }
  function beginTransition(type, channel, from, to, ms, fallback) {
    cancelTransitions();
    s[channel] = from;
    const duration = Number.isFinite(Number(ms)) ? Math.max(0, Number(ms)) : fallback;
    let complete;
    const promise = new Promise((done) => { complete = done; });
    transition = { type, channel, from, to, duration, t0: clock(), resolve: complete, settled: false };
    if (s.destroyed || s.contextLost || !s.running || hidden() || reduce() || duration === 0) {
      settleEndpoint(!s.destroyed);
    } else {
      render();
      schedule();
    }
    return promise;
  }
  function canonical(reveal) {
    cancelTransitions();
    s.time = 0;
    s.spin = 0;
    s.baseYaw = s.yaw = 0.40;
    s.basePitch = s.pitch = 0.17;
    s.reveal = s.pull = unit(reveal);
    s.assemble = 1;
    s.dissolve = 0;
    s.verify = s.deny = s.standby = 0;
    s.last = 0;
  }
  function redrawPaused() {
    if (!s.running) render();
  }
  function resume() {
    if (s.destroyed) return;
    if (!s.running) { s.running = true; s.last = 0; }
    schedule();
  }
  function visibilityChanged() {
    if (s.destroyed) return;
    if (hidden()) {
      cancelFrame();
      settleEndpoint(false);
    } else {
      s.last = 0;
      if (s.running) schedule();
      else render();
    }
  }
  function contextLost(event) {
    event.preventDefault();
    if (s.destroyed) return;
    s.contextLost = true;
    cancelFrame();
    settleEndpoint(false);
    // 丢失的 WebGL 对象已失效；恢复时重建，不继续使用旧句柄。
    resources = null;
  }
  function contextRestored() {
    if (s.destroyed) return;
    s.contextLost = false;
    if (!initResources()) { settleEndpoint(false); return; }
    s.last = 0;
    render();
    schedule();
  }
  doc.addEventListener("visibilitychange", visibilityChanged);
  canvas.addEventListener("webglcontextlost", contextLost);
  canvas.addEventListener("webglcontextrestored", contextRestored);

  return {
    __sceneStats: band.stats,
    start: resume,
    resume,
    stop() {
      s.running = false;
      cancelFrame();
      settleEndpoint();
    },
    reset({ reveal = 0 } = {}) {
      canonical(reveal);
      redrawPaused();
    },
    setReducedMotion(value) {
      reducedOverride = Boolean(value);
      s.last = 0;
      if (reducedOverride) settleEndpoint();
      else redrawPaused();
      schedule();
    },
    /** 0..1：玻璃出场状态，同时决定为登录面板让位的程度。 */
    setReveal(v) {
      s.reveal = s.pull = unit(v);
      redrawPaused();
    },
    setPull(v) { s.pull = unit(v); redrawPaused(); },
    setVerify(v) { s.verify = unit(v); redrawPaused(); },
    setDenied(v) { s.deny = unit(v); redrawPaused(); },
    setStandby(v) { s.standby = unit(v); redrawPaused(); },
    playAssemble(durationMs = 1800) {
      s.dissolve = 0;
      return beginTransition("assemble", "assemble", 0, 1, durationMs, 1800);
    },
    setAssembled() {
      cancelTransitions();
      s.assemble = 1;
      s.dissolve = 0;
      redrawPaused();
    },
    playDissolve(durationMs = 1500) {
      s.assemble = 1;
      s.verify = s.deny = s.standby = 0;
      return beginTransition("dissolve", "dissolve", 0, 1, durationMs, 1500);
    },
    playReassemble(durationMs = 1000) {
      // 保留父层 reset({reveal}) 的选择，其余状态（包括拖拽）一并归零。
      canonical(s.reveal);
      return beginTransition("reassemble", "dissolve", 1, 0, durationMs, 1000);
    },
    rotateBy(dx, dy) {
      s.baseYaw += (Number(dx) || 0) * 0.0075;
      s.basePitch = clamp(s.basePitch + (Number(dy) || 0) * 0.0048, -0.62, 0.85);
      s.yaw = s.baseYaw + Math.sin(s.time * 0.085) * 0.26;
      s.pitch = s.basePitch + Math.sin(s.time * 0.052) * 0.08;
      redrawPaused();
    },
    destroy() {
      if (s.destroyed) return;
      s.running = false;
      s.destroyed = true;
      cancelFrame();
      settleEndpoint(false);
      doc.removeEventListener("visibilitychange", visibilityChanged);
      canvas.removeEventListener("webglcontextlost", contextLost);
      canvas.removeEventListener("webglcontextrestored", contextRestored);
      releaseResources();
      if (canvas.__sceneStats === band.stats) delete canvas.__sceneStats;
      if (win.__sceneStats === band.stats) delete win.__sceneStats;
    },
  };
}

export function webglSupported() {
  try {
    const probe = document.createElement("canvas");
    const ctx = probe.getContext("webgl") || probe.getContext("experimental-webgl");
    if (!ctx) return false;
    // 无显卡的软件渲染（SwiftShader）也能跑，但过慢的话入口会卡
    return true;
  } catch (error) {
    return false;
  }
}

export const SCENE_READY = true;
