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
  const perCorner = Math.max(2, Math.round(RING / 4));
  const profile = sectionProfile(shape.corner, perCorner);
  const P = profile.length;
  const positions = [];
  const normals = [];
  const params = [];
  const indices = [];

  for (let s = 0; s <= SEG; s += 1) {
    const t = (s / SEG) * TAU;
    const center = curveAt(t, shape);
    const T = curveTangent(t, shape);
    // 以 +Z 为参考建立截面基
    const ref = [0, 0, 1];
    const N0 = normalize([
      ref[0] - dot(ref, T) * T[0],
      ref[1] - dot(ref, T) * T[1],
      ref[2] - dot(ref, T) * T[2],
    ]);
    const B0 = cross(T, N0);
    // 沿曲线渐进扭转，带子才不是一片死板的传送带
    const phi = shape.twist * t;
    const W = [
      Math.cos(phi) * N0[0] + Math.sin(phi) * B0[0],
      Math.cos(phi) * N0[1] + Math.sin(phi) * B0[1],
      Math.cos(phi) * N0[2] + Math.sin(phi) * B0[2],
    ];
    const H = cross(T, W);

    for (let r = 0; r < P; r += 1) {
      const px = profile[r][0];
      const py = profile[r][1];
      positions.push(
        center[0] + W[0] * px * halfWidth + H[0] * py * height,
        center[1] + W[1] * px * halfWidth + H[1] * py * height,
        center[2] + W[2] * px * halfWidth + H[2] * py * height,
      );
      // 截面法线：圆角轮廓上，从截面中心指向该点的方向是很好的近似
      const len = Math.hypot(px, py) || 1;
      const nx = px / len;
      const ny = py / len;
      normals.push(
        W[0] * nx + H[0] * ny,
        W[1] * nx + H[1] * ny,
        W[2] * nx + H[2] * ny,
      );
      params.push(s / SEG, r / P);
    }
  }

  for (let s = 0; s < SEG; s += 1) {
    for (let r = 0; r < P; r += 1) {
      const a = s * P + r;
      const b = s * P + ((r + 1) % P);
      const c = (s + 1) * P + ((r + 1) % P);
      const d = (s + 1) * P + r;
      indices.push(a, b, c, a, c, d);
    }
  }

  return {
    position: new Float32Array(positions),
    normal: new Float32Array(normals),
    param: new Float32Array(params),
    index: new Uint16Array(indices),
    count: indices.length,
    vertices: (SEG + 1) * P,
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
attribute vec2 aParam;
uniform mat4 uProj;
uniform mat4 uView;
uniform mat4 uModel;
uniform float uTime;
uniform float uAssemble;
uniform float uDissolve;
uniform float uReveal;
uniform float uWobble;
varying vec3 vWorld;
varying vec3 vNrm;
varying vec2 vParam;
varying float vTip;
varying float vSeed;

float b_hash(float p) {
  p = fract(p * 0.1031);
  p *= p + 33.33;
  p *= p + p;
  return fract(p);
}

void main() {
  vec3 p = aPos;
  float param = aParam.x;
  float seed = b_hash(floor(param * 487.0) * 1.618 + 4.7);

  // 液态呼吸：沿带子传播的缓慢形变。
  // 位移都以物体自身的尺度为单位（曲线半径 = 1），所以这里的系数都很小；
  // 参考作品里那些几十、几百的常数是按半径 285 的场景写的，直接搬会炸开。
  vec3 radial = normalize(p + vec3(0.0, 0.0, 0.001));
  float breath = sin(param * 6.2831853 * 2.0 + uTime * 0.62) * 0.6
               + sin(param * 6.2831853 * 5.0 - uTime * 0.41 + 1.7) * 0.4;
  p += radial * breath * uWobble * 0.022;

  // 装配：像笔尖沿带子把材料画出来
  float win = 0.085;
  float local = clamp((uAssemble * (1.0 + win) - param) / win, 0.0, 1.0);
  float settle = 1.0 - pow(1.0 - local, 3.0);
  float grow = local > 0.0
    ? 1.0 + 2.70158 * pow(local - 1.0, 3.0) + 1.70158 * pow(local - 1.0, 2.0)
    : 0.0;
  p = p * grow + normalize(p + vec3(0.0, 0.0, 40.0)) * (1.0 - settle) * 0.55;

  // 化散：解锁时逐段剥离并飞开
  float peel = clamp(uDissolve * 1.7 - param * 0.65 - seed * 0.22, 0.0, 1.0);
  float peelE = peel * peel * (3.0 - 2.0 * peel);
  vec3 away = normalize(vec3(seed - 0.5, 0.62, 0.36 + seed));
  p += away * peelE * (0.9 + seed * 1.1);
  p = mix(p, p * 0.72, peelE * 0.6);

  vec4 world = uModel * vec4(p, 1.0);
  vWorld = world.xyz;
  vNrm = mat3(uModel) * aNrm;
  vParam = aParam;
  float dt = param - uAssemble * (1.0 + win) + win * 0.5;
  vTip = exp(-dt * dt * 3200.0) * (1.0 - smoothstep(0.9, 1.0, uAssemble));
  vSeed = seed;
  gl_Position = uProj * uView * world;
}`;

const BAND_FS = `${COMMON_FS_HEAD}
varying vec3 vWorld;
varying vec3 vNrm;
varying vec2 vParam;
varying float vTip;
varying float vSeed;
uniform vec3 uEye;
uniform float uTime;
uniform float uAssemble;
uniform float uVerify;
uniform float uDeny;
uniform float uReveal;
uniform sampler2D uBgTex;
uniform vec2 uRes;

void main() {
  vec3 N = normalize(vNrm);
  vec3 V = normalize(uEye - vWorld);
  if (!gl_FrontFacing) N = -N;

  // 液面细纹：扰动一点点法线，反射才不是死平的镜子
  float ripple = sin(vParam.x * 197.0 + uTime * 1.35)
               + sin(vParam.x * 59.0 - uTime * 0.88 + vParam.y * 9.0) * 0.7;
  vec3 jitter = vec3(cos(vParam.y * 6.2831853) * ripple, sin(vParam.y * 6.2831853) * ripple, 0.0);
  N = normalize(N + jitter * 0.010 * (0.4 + vSeed * 0.8));

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
  float alpha = clamp(mix(0.30, 0.80, fres), 0.0, 1.0);
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
  if (!vs || !fs) return null;
  const program = gl.createProgram();
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.warn("液态金属程序链接失败：", gl.getProgramInfoLog(program));
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

  const bgProgram = link(gl, BACKGROUND_VS, BACKGROUND_FS);
  const bandProgram = link(gl, BAND_VS, BAND_FS);
  if (!bgProgram || !bandProgram) return null;

  const band = buildBand(SHAPE);
  const reduce = () => Boolean(options.reduce && options.reduce());

  const quadBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);

  // 背景先画进这张离屏纹理，玻璃再按法线偏移去采样它。
  // 这是"折射"的关键：玻璃后面的背景真的被扭曲，而不是只加一层半透明色。
  const bgTex = gl.createTexture();
  const bgFbo = gl.createFramebuffer();
  let bgTexW = 0, bgTexH = 0;
  function ensureBgTarget(w, h) {
    if (bgTexW === w && bgTexH === h) return;
    bgTexW = w; bgTexH = h;
    gl.bindTexture(gl.TEXTURE_2D, bgTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindFramebuffer(gl.FRAMEBUFFER, bgFbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, bgTex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  const positionBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, band.position, gl.STATIC_DRAW);
  const normalBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, normalBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, band.normal, gl.STATIC_DRAW);
  const paramBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, paramBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, band.param, gl.STATIC_DRAW);
  const indexBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, band.index, gl.STATIC_DRAW);

  const u = {
    bgPos: gl.getAttribLocation(bgProgram, "aPos"),
    bgRes: gl.getUniformLocation(bgProgram, "uRes"),
    bgTime: gl.getUniformLocation(bgProgram, "uTime"),
    bgReveal: gl.getUniformLocation(bgProgram, "uReveal"),
    bgVerify: gl.getUniformLocation(bgProgram, "uVerify"),
    bgDeny: gl.getUniformLocation(bgProgram, "uDeny"),
    bgStandby: gl.getUniformLocation(bgProgram, "uStandby"),
    bgTex: gl.getUniformLocation(bgProgram, "uBgTex"),

    pos: gl.getAttribLocation(bandProgram, "aPos"),
    nrm: gl.getAttribLocation(bandProgram, "aNrm"),
    prm: gl.getAttribLocation(bandProgram, "aParam"),
    proj: gl.getUniformLocation(bandProgram, "uProj"),
    view: gl.getUniformLocation(bandProgram, "uView"),
    model: gl.getUniformLocation(bandProgram, "uModel"),
    eye: gl.getUniformLocation(bandProgram, "uEye"),
    time: gl.getUniformLocation(bandProgram, "uTime"),
    assemble: gl.getUniformLocation(bandProgram, "uAssemble"),
    dissolve: gl.getUniformLocation(bandProgram, "uDissolve"),
    verify: gl.getUniformLocation(bandProgram, "uVerify"),
    deny: gl.getUniformLocation(bandProgram, "uDeny"),
    reveal: gl.getUniformLocation(bandProgram, "uReveal"),
    bgTex: gl.getUniformLocation(bandProgram, "uBgTex"),
    res: gl.getUniformLocation(bandProgram, "uRes"),
    wobble: gl.getUniformLocation(bandProgram, "uWobble"),
  };

  const s = {
    time: 0,
    yaw: 0.40,
    pitch: 0.17,
    baseYaw: 0.40,
    basePitch: 0.17,
    spin: 0,
    spinSpeed: 0.14,
    reveal: 0,
    pull: 0,
    assemble: 0,
    assembleAnim: null,
    dissolve: 0,
    dissolveAnim: null,
    verify: 0,
    deny: 0,
    standby: 0,
    running: false,
    raf: 0,
    last: 0,
  };

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.floor((rect.width || window.innerWidth) * dpr));
    const h = Math.max(1, Math.floor((rect.height || window.innerHeight) * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
  }

  function frame(now) {
    if (!s.running) return;
    s.raf = requestAnimationFrame(frame);
    const dt = s.last ? Math.min(0.05, (now - s.last) / 1000) : 0.016;
    s.last = now;
    const still = reduce();
    if (!still) s.time += dt;

    resize();
    const W = canvas.width;
    const H = canvas.height;
    gl.viewport(0, 0, W, H);
    // 深度缓冲每帧都要清：背景只是盖上去的颜色，金属仍按深度排序
    gl.clearColor(0, 0, 0, 1);
    gl.clearDepth(1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const aspect = W / Math.max(H, 1);

    // 动效推进
    if (s.assembleAnim) {
      const a = s.assembleAnim;
      const x = Math.min(1, (s.time - a.t0) / a.dur);
      s.assemble = a.from + (a.to - a.from) * (0.5 - 0.5 * Math.cos(Math.PI * x));
      if (x >= 1) s.assembleAnim = null;
    }
    if (s.dissolveAnim) {
      const x = Math.min(1, (s.time - s.dissolveAnim.t0) / s.dissolveAnim.dur);
      s.dissolve = x * x;
    }
    if (!still) {
      s.spin += dt * s.spinSpeed;
      s.yaw = s.baseYaw + Math.sin(s.time * 0.085) * 0.26;
      s.pitch = s.basePitch + Math.sin(s.time * 0.052 + 0.8) * 0.08;
    }

    // 相机：物体横向半径约 1.1，让整条带子留出边距落进画面。
    // 纵向要容纳标题，所以整体往上抬、稍微拉远。
    // 窄屏（手机）画面更高更窄，拉近一点让它占满上半屏。
    const narrow = aspect < 0.85;
    const fov = narrow ? 0.78 : 0.92;
    const tan = Math.tan(fov / 2);
    const needH = 1.46 / tan;
    const needW = 1.46 / (tan * aspect);
    const dist = Math.max(needH, needW) * (narrow ? 0.92 : 1.06);
    const eye = [0, narrow ? 0.10 : 0.16, dist];
    // 窄屏把注视点抬高，玻璃整体上移到上半屏（文案在它下方）
    const target = narrow ? [0, -0.42, 0] : [0, 0, 0];
    const proj = perspective(fov, aspect, 0.1, 60);
    const view = lookAt(eye, target, [0, 1, 0]);

    // ── 背景：先画进离屏纹理（玻璃要折射它） ──
    ensureBgTarget(W, H);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);
    gl.useProgram(bgProgram);
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.enableVertexAttribArray(u.bgPos);
    gl.vertexAttribPointer(u.bgPos, 2, gl.FLOAT, false, 0, 0);
    gl.uniform2f(u.bgRes, W, H);
    gl.uniform1f(u.bgTime, still ? 0 : s.time);
    gl.uniform1f(u.bgReveal, s.reveal);
    gl.uniform1f(u.bgVerify, s.verify);
    gl.uniform1f(u.bgDeny, s.deny);
    gl.uniform1f(u.bgStandby, s.standby);
    // 画进离屏之前必须把这张纹理从采样单元上解掉，
    // 否则它同时是"绘制目标"和"采样源"，GL 会报 INVALID_OPERATION
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, bgFbo);
    gl.viewport(0, 0, W, H);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    // 再把这张背景铺回屏幕，同时留给玻璃采样
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, W, H);
    gl.bindTexture(gl.TEXTURE_2D, bgTex);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    // ── 玻璃体 ──
    gl.enable(gl.DEPTH_TEST);
    gl.depthMask(false);
    // 玻璃要真的透出背景：用标准 alpha 混合。
    // 不能用 (ONE, ONE_MINUS_SRC_ALPHA)——那是加法混合，会把亮面烧白。
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    // 双面绘制：前后两层玻璃重叠，自然叠出厚度
    gl.disable(gl.CULL_FACE);
    gl.useProgram(bandProgram);

    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.enableVertexAttribArray(u.pos);
    gl.vertexAttribPointer(u.pos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, normalBuffer);
    gl.enableVertexAttribArray(u.nrm);
    gl.vertexAttribPointer(u.nrm, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, paramBuffer);
    gl.enableVertexAttribArray(u.prm);
    gl.vertexAttribPointer(u.prm, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);

    // 居中时略偏左；登录面板展开后让到左侧三分之一处。
    // 窄屏画面窄，横向让位幅度要收小，否则玻璃会被挤出画面。
    const shiftX = narrow ? (-0.02 - s.pull * 0.10) : (-0.06 - s.pull * 0.46);
    // 抬到画面中上部，标题落在下方而不是压在中心。
    // 窄屏的注视点已经抬高过，这里只做小幅让位。
    const lift = (narrow ? 0.06 : 0.62) + s.pull * (narrow ? 0.02 : 0.06);
    const model = multiply(
      translation(shiftX, lift, 0),
      multiply(
        rotationY(s.yaw + s.spin),
        multiply(rotationX(s.pitch), rotationZ(-0.07)),
      ),
    );

    gl.uniformMatrix4fv(u.proj, false, proj);
    gl.uniformMatrix4fv(u.view, false, view);
    gl.uniformMatrix4fv(u.model, false, model);
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
  }

  return {
    start() {
      if (s.running) return;
      s.running = true;
      s.last = 0;
      s.raf = requestAnimationFrame(frame);
    },
    stop() {
      s.running = false;
      cancelAnimationFrame(s.raf);
    },
    /** 0..1：金属的出场进度（影响透明度），同时决定让位程度 */
    setReveal(v) {
      const x = clamp(v, 0, 1);
      s.reveal = x;
      s.pull = x;
    },
    /** 只控制让位，不改出场透明度——滚轮切换时用 */
    setPull(v) {
      s.pull = clamp(v, 0, 1);
    },
    setVerify(v) {
      s.verify = clamp(v, 0, 1);
    },
    setDenied(v) {
      s.deny = clamp(v, 0, 1);
    },
    setStandby(v) {
      s.standby = clamp(v, 0, 1);
    },
    playAssemble(ms = 1800) {
      s.assemble = 0;
      s.assembleAnim = { from: 0, to: 1, t0: s.time, dur: ms / 1000 };
    },
    setAssembled() {
      s.assemble = 1;
      s.assembleAnim = null;
    },
    playDissolve(ms = 900) {
      s.dissolve = 0;
      s.dissolveAnim = { t0: s.time, dur: ms / 1000 };
    },
    /** 拖拽旋转 */
    rotateBy(dx, dy) {
      s.baseYaw = s.yaw + dx * 0.0075;
      s.basePitch = clamp(s.pitch + dy * 0.0048, -0.62, 0.85);
      s.yaw = s.baseYaw;
      s.pitch = s.basePitch;
    },
    destroy() {
      this.stop();
      gl.deleteBuffer(quadBuffer);
      gl.deleteBuffer(positionBuffer);
      gl.deleteBuffer(normalBuffer);
      gl.deleteBuffer(paramBuffer);
      gl.deleteBuffer(indexBuffer);
      gl.deleteProgram(bgProgram);
      gl.deleteProgram(bandProgram);
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
