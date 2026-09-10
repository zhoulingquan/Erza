import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { createPortal } from "react-dom";
import {
  Check,
  Circle,
  CornerDownRight,
  Pencil,
  Square,
  Type,
  Undo2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

export interface ScreenCaptureOverlayProps {
  onComplete: (file: File) => void;
  onCancel: () => void;
}

type Tool = "rect" | "ellipse" | "arrow" | "pencil" | "text";

interface Point {
  x: number;
  y: number;
}

/** 已提交的标注图形(坐标均为选中区域内的原生像素)。 */
type Shape =
  | { kind: "rect"; a: Point; b: Point }
  | { kind: "ellipse"; a: Point; b: Point }
  | { kind: "arrow"; a: Point; b: Point }
  | { kind: "pencil"; pts: Point[] }
  | { kind: "text"; p: Point; text: string; size: number };

/** 正在绘制中的图形 / 铅笔路径。 */
type Draft =
  | { kind: "rect"; a: Point; b: Point }
  | { kind: "ellipse"; a: Point; b: Point }
  | { kind: "arrow"; a: Point; b: Point }
  | { kind: "pencil"; pts: Point[] };

const TOOLS: Array<{ tool: Tool; icon: typeof Square; key: string }> = [
  { tool: "rect", icon: Square, key: "rect" },
  { tool: "ellipse", icon: Circle, key: "ellipse" },
  { tool: "arrow", icon: CornerDownRight, key: "arrow" },
  { tool: "pencil", icon: Pencil, key: "pencil" },
  { tool: "text", icon: Type, key: "text" },
];

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob | null> {
  return new Promise((resolve) => {
    canvas.toBlob(resolve, "image/png");
  });
}

/** 计算 video 在 overlay 内按 object-fit: contain 渲染出的可视矩形(overlay 坐标)。 */
function getDisplayRect(el: {
  videoWidth: number;
  videoHeight: number;
  clientWidth: number;
  clientHeight: number;
}) {
  const scale = Math.min(
    el.clientWidth / el.videoWidth,
    el.clientHeight / el.videoHeight,
  );
  const dw = el.videoWidth * scale;
  const dh = el.videoHeight * scale;
  return {
    left: (el.clientWidth - dw) / 2,
    top: (el.clientHeight - dh) / 2,
    width: dw,
    height: dh,
  };
}

// ─────────────────────────────────────────────────────────────
// 持久屏幕捕获流
//
// getDisplayMedia 每次调用都会弹出系统选择卡,且没有持久授权,这是浏览器的
// 安全边界,无法绕过。为了尽量接近微信「点一下即进入截图状态」的体验:
// 首次截图时弹出系统卡让用户选「整个屏幕」,随后把该流保持存活并全局复用,
// 之后所有截图不再弹卡,直接进入选区框。用户可随时用浏览器工具栏的
// 「停止共享」结束该流。
// ─────────────────────────────────────────────────────────────

let sharedStream: MediaStream | null = null;

type AcquireResult =
  | { ok: true; stream: MediaStream }
  | { ok: false; reason: "not_supported" | "denied" };

async function acquireStream(): Promise<AcquireResult> {
  const track = sharedStream?.getVideoTracks().find((t) => t.readyState === "live");
  if (sharedStream && track) {
    return { ok: true, stream: sharedStream };
  }
  sharedStream = null;

  if (!navigator.mediaDevices?.getDisplayMedia) {
    return { ok: false, reason: "not_supported" };
  }
  try {
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: {
        frameRate: { ideal: 2, max: 4 },
      },
      audio: false,
    });
    sharedStream = stream;
    // 全部轨道结束后清空全局缓存,下次截图再重新弹卡授权
    const clearIfEnded = () => {
      if (sharedStream && sharedStream.getTracks().every((t) => t.readyState === "ended")) {
        sharedStream = null;
      }
    };
    stream.getTracks().forEach((t) => t.addEventListener("ended", clearIfEnded));
    return { ok: true, stream };
  } catch {
    return { ok: false, reason: "denied" };
  }
}

let uid = 0;
function nextId(): number {
  uid += 1;
  return uid;
}

/** 全屏区域截图 Overlay,交互参照微信截图:
 * 捕获屏幕(首次弹系统卡,之后复用持久流)→ 全屏调暗供拖选区域 →
 * 选区下方弹出标注工具栏(矩形/椭圆/箭头/铅笔/文字/撤销/取消/发送),
 * 确认后按原分辨率裁剪当前帧并烘焙标注为 PNG。 */
export function ScreenCaptureOverlay({ onComplete, onCancel }: ScreenCaptureOverlayProps) {
  const { t } = useTranslation();

  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 选区(overlay 坐标,记录拖选时的两个角点)
  const selStartRef = useRef<Point | null>(null);
  const selEndRef = useRef<Point | null>(null);
  const [sel, setSel] = useState<{ x: number; y: number; w: number; h: number } | null>(null);

  const [mode, setMode] = useState<"select" | "annotate">("select");
  const [tool, setTool] = useState<Tool>("rect");

  // 标注
  const annoRef = useRef<HTMLCanvasElement>(null);
  const shapesRef = useRef<Shape[]>([]);
  const draftRef = useRef<Draft | null>(null);
  const [shapesVersion, setShapesVersion] = useState(0);
  const [textInput, setTextInput] = useState<{ id: number; x: number; y: number } | null>(null);
  const textRef = useRef<HTMLInputElement>(null);

  // 获取(或复用)捕获流并挂到 video
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const result = await acquireStream();
      if (cancelled) return;
      if (!result.ok) {
        setError(result.reason);
        return;
      }
      const video = videoRef.current;
      if (video) {
        video.srcObject = result.stream;
        video.play().catch(() => {});
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onData = () => setReady(video.videoWidth > 0 && video.videoHeight > 0);
    video.addEventListener("loadeddata", onData);
    return () => video.removeEventListener("loadeddata", onData);
  }, []);

  // 把 overlay 坐标映射到屏幕(原生)坐标
  const toNative = useCallback((p: Point): Point => {
    const video = videoRef.current;
    const container = containerRef.current;
    if (!video || !container) return p;
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh) return p;
    const videoRect = getDisplayRect({
      videoWidth: vw,
      videoHeight: vh,
      clientWidth: container.clientWidth,
      clientHeight: container.clientHeight,
    });
    return {
      x: ((p.x - videoRect.left) / videoRect.width) * vw,
      y: ((p.y - videoRect.top) / videoRect.height) * vh,
    };
  }, []);

  // 把 overlay 坐标的选区转为原生像素选区
  const nativeSelection = useCallback((): { x: number; y: number; w: number; h: number } | null => {
    if (!selStartRef.current || !selEndRef.current) return null;
    const a = toNative(selStartRef.current);
    const b = toNative(selEndRef.current);
    return {
      x: Math.min(a.x, b.x),
      y: Math.min(a.y, b.y),
      w: Math.abs(b.x - a.x),
      h: Math.abs(b.y - a.y),
    };
  }, [toNative]);

  // ---- 拖选 ----
  const onSelectPointerDown = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    selStartRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    selEndRef.current = null;
    setSel(null);
  }, []);

  const onSelectPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (!selStartRef.current) return;
      const rect = e.currentTarget.getBoundingClientRect();
      selEndRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      const a = selStartRef.current;
      const b = selEndRef.current;
      setSel({
        x: Math.min(a.x, b.x),
        y: Math.min(a.y, b.y),
        w: Math.abs(b.x - a.x),
        h: Math.abs(b.y - a.y),
      });
    },
    [],
  );

  const onSelectPointerUp = useCallback(() => {
    if (!sel || sel.w < 2 || sel.h < 2) {
      selStartRef.current = null;
      selEndRef.current = null;
      setSel(null);
      return;
    }
    setMode("annotate");
  }, [sel]);

  // 标注:绘制颜色与线宽(原生像素)
  const paintCtx = useCallback(() => {
    const canvas = annoRef.current;
    if (!canvas) return null;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    const virtualScale = canvas.width / (canvas.clientWidth || 1);
    ctx.lineWidth = Math.max(2, 4 * virtualScale);
    ctx.strokeStyle = "#ef4444";
    ctx.fillStyle = "#ef4444";
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    return ctx;
  }, []);

  const drawShape = useCallback(
    (ctx: CanvasRenderingContext2D | null, shape: Shape) => {
      if (!ctx) return;
      ctx.beginPath();
      if (shape.kind === "rect") {
        ctx.strokeRect(shape.a.x, shape.a.y, shape.b.x - shape.a.x, shape.b.y - shape.a.y);
      } else if (shape.kind === "ellipse") {
        const cx = (shape.a.x + shape.b.x) / 2;
        const cy = (shape.a.y + shape.b.y) / 2;
        ctx.ellipse(cx, cy, Math.abs(shape.b.x - shape.a.x) / 2, Math.abs(shape.b.y - shape.a.y) / 2, 0, 0, Math.PI * 2);
        ctx.stroke();
      } else if (shape.kind === "arrow") {
        ctx.moveTo(shape.a.x, shape.a.y);
        ctx.lineTo(shape.b.x, shape.b.y);
        ctx.stroke();
        const angle = Math.atan2(shape.b.y - shape.a.y, shape.b.x - shape.a.x);
        const size = Math.max(6, 19);
        ctx.beginPath();
        ctx.moveTo(shape.b.x, shape.b.y);
        ctx.lineTo(shape.b.x - size * Math.cos(angle - 0.45), shape.b.y - size * Math.sin(angle - 0.45));
        ctx.lineTo(shape.b.x - size * Math.cos(angle + 0.45), shape.b.y - size * Math.sin(angle + 0.45));
        ctx.closePath();
        ctx.fill();
      } else if (shape.kind === "pencil") {
        if (shape.pts.length < 1) return;
        ctx.moveTo(shape.pts[0].x, shape.pts[0].y);
        for (const p of shape.pts) ctx.lineTo(p.x, p.y);
        ctx.stroke();
      } else if (shape.kind === "text") {
        ctx.font = `${shape.size}px system-ui, sans-serif`;
        ctx.fillText(shape.text, shape.p.x, shape.p.y);
      }
    },
    [],
  );

  const redrawShapes = useCallback(() => {
    const canvas = annoRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const paintCtxLocal = paintCtx();
    if (!paintCtxLocal) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    // Annotation canvas may be larger than display size; ensure full clear.
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const s of shapesRef.current) drawShape(ctx, s);
    if (draftRef.current) {
      if (draftRef.current.kind === "pencil") {
        drawShape(ctx, { kind: "pencil", pts: draftRef.current.pts });
      } else {
        drawShape(ctx, draftRef.current);
      }
    }
  }, [drawShape, paintCtx]);

  // 触发一次重绘(shapes 提交 / 尺寸改变后)
  useEffect(() => {
    redrawShapes();
  }, [shapesVersion, redrawShapes]);

  // 进入标注模式时,按选区原生尺寸初始化标注 canvas 并铺满选区显示
  useEffect(() => {
    if (mode !== "annotate") return;
    const native = nativeSelection();
    const canvas = annoRef.current;
    if (!native || !canvas) return;
    canvas.width = Math.max(1, Math.round(native.w));
    canvas.height = Math.max(1, Math.round(native.h));
    redrawShapes();
  }, [mode, nativeSelection, redrawShapes]);

  const annoToNative = useCallback(
    (e: ReactPointerEvent<HTMLCanvasElement>): Point => {
      const canvas = annoRef.current;
      if (!canvas) return { x: 0, y: 0 };
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
      return {
        x: (e.clientX - rect.left) * scaleX,
        y: (e.clientY - rect.top) * scaleY,
      };
    },
    [],
  );

  const onAnnotatePointerDown = useCallback(
    (e: ReactPointerEvent<HTMLCanvasElement>) => {
      if (tool === "text") {
        const p = annoToNative(e);
        setTextInput({ id: nextId(), x: p.x, y: p.y });
        return;
      }
      const p = annoToNative(e);
      if (tool === "pencil") {
        draftRef.current = { kind: "pencil", pts: [p] };
      } else {
        draftRef.current = { kind: tool, a: p, b: p };
      }
      e.currentTarget.setPointerCapture(e.pointerId);
    },
    [annoToNative, tool],
  );

  const onAnnotatePointerMove = useCallback(
    (e: ReactPointerEvent<HTMLCanvasElement>) => {
      const d = draftRef.current;
      if (!d) return;
      const p = annoToNative(e);
      if (d.kind === "pencil") {
        d.pts = [...d.pts, p];
      } else {
        d.b = { ...p };
      }
      redrawShapes();
    },
    [annoToNative, redrawShapes],
  );

  const onAnnotatePointerUp = useCallback(() => {
    const d = draftRef.current;
    if (d) {
      if (d.kind !== "pencil" && Math.abs(d.b.x - d.a.x) > 1 && Math.abs(d.b.y - d.a.y) > 1) {
        shapesRef.current = [...shapesRef.current, { ...d }];
      } else if (d.kind === "pencil" && d.pts.length > 0) {
        shapesRef.current = [...shapesRef.current, { kind: "pencil", pts: d.pts }];
      }
      draftRef.current = null;
      setShapesVersion((v) => v + 1);
    }
  }, []);

  const commitText = useCallback(
    (displayText: string) => {
      if (!textInput) return;
      const video = videoRef.current;
      const container = containerRef.current;
      const clientH = container?.clientHeight || 1;
      const nativeH = video?.videoHeight || 0;
      const scaleY = clientH > 0 ? nativeH / clientH : 1;
      const size = Math.max(12, Math.round(16 * scaleY));
      const textLib = displayText.trim();
      if (textLib) {
        shapesRef.current = [...shapesRef.current, {
          kind: "text",
          p: { x: textInput.x, y: textInput.y },
          text: textLib,
          size,
        }];
        setShapesVersion((v) => v + 1);
      }
      setTextInput(null);
    },
    [textInput],
  );

  const undo = useCallback(() => {
    shapesRef.current = shapesRef.current.slice(0, -1);
    setShapesVersion((v) => v + 1);
  }, []);

  const cancel = useCallback(() => {
    onCancel();
  }, [onCancel]);

  const confirm = useCallback(() => {
    const video = videoRef.current;
    const native = nativeSelection();
    if (!video || !native || native.w < 2 || native.h < 2) {
      setError("empty");
      return;
    }
    void (async () => {
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      if (!vw || !vh) {
        setError("empty");
        return;
      }
      const out = document.createElement("canvas");
      out.width = Math.round(native.w);
      out.height = Math.round(native.h);
      const ctx = out.getContext("2d");
      if (!ctx) return;
      ctx.drawImage(video, native.x, native.y, native.w, native.h, 0, 0, out.width, out.height);
      const anno = annoRef.current;
      if (anno && anno.width > 0) {
        ctx.drawImage(anno, 0, 0);
      }
      const blob = await canvasToBlob(out);
      if (!blob) {
        setError("empty");
        return;
      }
      onComplete(new File([blob], `screenshot-${Date.now()}.png`, { type: "image/png" }));
    })();
  }, [nativeSelection, onComplete]);

  // Esc 取消
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (textInput) {
          setTextInput(null);
        } else {
          cancel();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cancel, textInput]);

  // 文字输入框出现时置焦
  useEffect(() => {
    if (textInput) {
      requestAnimationFrame(() => textRef.current?.focus());
    }
  }, [textInput]);

  return createPortal(
    <div
      ref={containerRef}
      className="fixed inset-0 z-[100] select-none"
      role="dialog"
      aria-modal="true"
      aria-label={t("thread.composer.screenshot.dialogAria")}
    >
      {/* 实时画面 + 调暗 / 亮区 */}
      <div
        onPointerDown={onSelectPointerDown}
        onPointerMove={onSelectPointerMove}
        onPointerUp={onSelectPointerUp}
        className="absolute inset-0 cursor-crosshair overflow-hidden bg-black"
      >
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className="h-full w-full object-contain"
        />
        {ready && !sel ? <div className="absolute inset-0 bg-black/45" /> : null}
        {ready && sel ? (
          <div
            className="absolute border-2 border-white"
            style={{
              left: sel.x,
              top: sel.y,
              width: sel.w,
              height: sel.h,
              boxShadow: "0 0 0 100vmax rgba(0,0,0,0.5)",
            }}
          />
        ) : null}

        {mode === "annotate" && sel ? (
          <div
            className="absolute overflow-hidden"
            style={{ left: sel.x, top: sel.y, width: sel.w, height: sel.h }}
          >
            <canvas
              ref={annoRef}
              onPointerDown={onAnnotatePointerDown}
              onPointerMove={onAnnotatePointerMove}
              onPointerUp={onAnnotatePointerUp}
              className="absolute left-0 top-0"
              style={{ width: "100%", height: "100%", cursor: tool === "text" ? "text" : "crosshair" }}
            />
          </div>
        ) : null}
      </div>

      {/* 顶部提示 */}
      {!ready ? (
        <div className="pointer-events-none absolute inset-x-0 top-6 flex justify-center">
          <p className="rounded-full bg-black/60 px-4 py-1.5 text-[13px] text-white backdrop-blur">
            {t("thread.composer.screenshot.loadingHint")}
          </p>
        </div>
      ) : mode === "select" ? (
        <div className="pointer-events-none absolute inset-x-0 top-6 flex flex-col items-center gap-2">
          <p className="rounded-full bg-black/60 px-4 py-1.5 text-[13px] text-white backdrop-blur">
            {t("thread.composer.screenshot.dragHint")}
          </p>
          <p className="rounded-full bg-black/40 px-3 py-1 text-[11.5px] text-white/70 backdrop-blur">
            {t("thread.composer.screenshot.sharingHint")}
          </p>
        </div>
      ) : null}

      {/* 文字输入 */}
      {textInput && sel ? (
        <input
          ref={textRef}
          defaultValue=""
          placeholder={t("thread.composer.screenshot.textPlaceholder")}
          className="absolute z-10 rounded border border-white/70 bg-black/70 px-2 py-1 text-white outline-none"
          style={{
            left:
              sel.x +
              (textInput.x / (annoRef.current?.clientWidth || 1)) * sel.w,
            top:
              sel.y +
              (textInput.y / (annoRef.current?.clientHeight || 1)) * sel.h,
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitText(e.currentTarget.value);
            if (e.key === "Escape") setTextInput(null);
          }}
          onBlur={() => setTextInput(null)}
        />
      ) : null}

      {/* 标注工具栏 */}
      {mode === "annotate" && sel ? (
        <div
          className="absolute z-20 flex items-center gap-1 rounded-xl border border-white/15 bg-black/80 p-1 backdrop-blur"
          style={{
            left: sel.x + sel.w / 2,
            top: sel.y + sel.h + 12,
            transform: "translateX(-50%)",
          }}
        >
          {TOOLS.map(({ tool: tk, icon: Icon, key }) => (
            <button
              key={key}
              type="button"
              onClick={() => setTool(tk)}
              aria-label={t(`thread.composer.screenshot.tool.${key}`)}
              aria-pressed={tool === tk}
              className={cn(
                "grid h-8 w-8 place-items-center rounded-lg text-white transition-colors",
                tool === tk ? "bg-white/20" : "hover:bg-white/10",
              )}
            >
              <Icon className="h-4 w-4" />
            </button>
          ))}
          <span className="mx-1 h-5 w-px bg-white/20" />
          <button
            type="button"
            onClick={undo}
            disabled={shapesRef.current.length === 0}
            aria-label={t("thread.composer.screenshot.undo")}
            className="grid h-8 w-8 place-items-center rounded-lg text-white transition-colors hover:bg-white/10 disabled:opacity-40"
          >
            <Undo2 className="h-4 w-4" />
          </button>
          <span className="mx-1 h-5 w-px bg-white/20" />
          <button
            type="button"
            onClick={cancel}
            aria-label={t("thread.composer.screenshot.cancel")}
            className="grid h-8 w-8 place-items-center rounded-lg text-white transition-colors hover:bg-white/10"
          >
            <X className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={confirm}
            aria-label={t("thread.composer.screenshot.confirm")}
            className="ml-0.5 flex items-center gap-1.5 rounded-lg bg-white px-3 py-2 text-[12.5px] font-semibold text-black transition-colors hover:bg-white/90"
          >
            <Check className="h-4 w-4" />
            {t("thread.composer.screenshot.confirm")}
          </button>
        </div>
      ) : null}

      {/* 错误提示 */}
      {error ? (
        <div className="absolute inset-x-0 bottom-24 flex justify-center">
          <p role="alert" className="rounded-md bg-destructive/90 px-3 py-1.5 text-[12.5px] text-white">
            {error === "denied"
              ? t("thread.composer.screenshot.errorDenied")
              : error === "not_supported"
                ? t("thread.composer.screenshot.errorNotSupported")
                : t("thread.composer.screenshot.errorEmpty")}
          </p>
        </div>
      ) : null}
    </div>,
    document.body,
  );
}