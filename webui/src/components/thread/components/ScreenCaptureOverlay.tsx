import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { createPortal } from "react-dom";
import { Check, X } from "lucide-react";
import { useTranslation } from "react-i18next";

export interface ScreenCaptureOverlayProps {
  onComplete: (file: File) => void;
  onCancel: () => void;
  /** 后端系统级截图的 objectURL(本地部署直达模式)。
   * 提供时以静态图片代替实时屏幕流:与微信截图一致,框选的是
   * 点击瞬间的屏幕快照;为空时回退 getDisplayMedia 实时流。 */
  imageSrc?: string | null;
}

interface Point {
  x: number;
  y: number;
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob | null> {
  return new Promise((resolve) => {
    canvas.toBlob(resolve, "image/png");
  });
}

/** 计算 capture 源(video 或 img)在 overlay 内按 object-fit: contain
 * 渲染出的可视矩形(overlay 坐标)。 */
function getDisplayRect(el: {
  width: number;
  height: number;
  clientWidth: number;
  clientHeight: number;
}) {
  const scale = Math.min(
    el.clientWidth / el.width,
    el.clientHeight / el.height,
  );
  const dw = el.width * scale;
  const dh = el.height * scale;
  return {
    left: (el.clientWidth - dw) / 2,
    top: (el.clientHeight - dh) / 2,
    width: dw,
    height: dh,
  };
}

/** 确认工具栏定位:默认在选区下方;选区贴近视口底部时翻转到选区上方,
 * 水平方向夹紧在视口内(否则确认按钮可能被挤出屏幕,用户无从点击)。 */
function toolbarStyle(
  sel: { x: number; y: number; w: number; h: number },
  container: HTMLElement | null,
): React.CSSProperties {
  const cw = container?.clientWidth ?? window.innerWidth;
  const ch = container?.clientHeight ?? window.innerHeight;
  const left = Math.min(Math.max(sel.x + sel.w / 2, 100), Math.max(cw - 100, 100));
  if (sel.y + sel.h + 60 <= ch) {
    return { left, top: sel.y + sel.h + 12, transform: "translateX(-50%)" };
  }
  return { left, top: sel.y - 12, transform: "translate(-50%, -100%)" };
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

/** 全屏区域截图 Overlay,交互参照微信截图:
 * 捕获屏幕(本地部署走后端系统截图直出快照;否则 getDisplayMedia 首次
 * 弹系统卡后复用持久流)→ 全屏调暗供拖选区域 →
 * 选区下方弹出「取消/发送」工具栏,确认后按原分辨率裁剪当前帧为 PNG。 */
export function ScreenCaptureOverlay({ onComplete, onCancel, imageSrc = null }: ScreenCaptureOverlayProps) {
  const { t } = useTranslation();

  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 当前捕获源(video 流或后端截图 img)的原生像素尺寸
  const sourceSize = useCallback((): { w: number; h: number } => {
    if (imageSrc) {
      const img = imgRef.current;
      return { w: img?.naturalWidth || 0, h: img?.naturalHeight || 0 };
    }
    const video = videoRef.current;
    return { w: video?.videoWidth || 0, h: video?.videoHeight || 0 };
  }, [imageSrc]);

  // 选区(overlay 坐标,记录拖选时的两个角点)
  const selStartRef = useRef<Point | null>(null);
  const selEndRef = useRef<Point | null>(null);
  const [sel, setSel] = useState<{ x: number; y: number; w: number; h: number } | null>(null);

  // select: 拖选中;confirm: 选区已锁定,展示取消/发送工具栏
  const [mode, setMode] = useState<"select" | "confirm">("select");

  // 获取(或复用)捕获流并挂到 video(后端截图模式下跳过)
  useEffect(() => {
    if (imageSrc) return;
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
  }, [imageSrc]);

  // ready 检测:video 等 loadeddata;img 由 JSX 的 onLoad 直接置位,
  // 这里仅在渲染后同步兜底一次(图片可能已被浏览器解码缓存)。
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onData = () => setReady(video.videoWidth > 0 && video.videoHeight > 0);
    video.addEventListener("loadeddata", onData);
    return () => video.removeEventListener("loadeddata", onData);
  }, []);

  useEffect(() => {
    if (!imageSrc) return;
    const img = imgRef.current;
    if (img?.complete && img.naturalWidth > 0) {
      setReady(true);
    }
  }, [imageSrc]);

  // 把 overlay 坐标映射到屏幕(原生)坐标
  const toNative = useCallback((p: Point): Point => {
    const container = containerRef.current;
    const { w: vw, h: vh } = sourceSize();
    if (!container || !vw || !vh) return p;
    const videoRect = getDisplayRect({
      width: vw,
      height: vh,
      clientWidth: container.clientWidth,
      clientHeight: container.clientHeight,
    });
    return {
      x: ((p.x - videoRect.left) / videoRect.width) * vw,
      y: ((p.y - videoRect.top) / videoRect.height) * vh,
    };
  }, [sourceSize]);

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
  // 拖选进行中标记:只有按住主键拖动才更新选区。松开后 selStartRef 仍保留
  // (裁剪要用),若不设此标记,松开后的鼠标移动会持续改写选区,选区框和
  // 工具栏跟着鼠标漂移,确认按钮根本无法点击。
  const selectDraggingRef = useRef(false);

  const onSelectPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (e.button !== 0) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const p = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      // 选区已锁定时,选区内按下不重置选区(防误触丢失);选区外按下 =
      // 放弃当前选区,重新拖选。
      if (
        mode === "confirm" &&
        sel &&
        p.x >= sel.x &&
        p.x <= sel.x + sel.w &&
        p.y >= sel.y &&
        p.y <= sel.y + sel.h
      ) {
        return;
      }
      selectDraggingRef.current = true;
      selStartRef.current = p;
      selEndRef.current = null;
      setSel(null);
      setMode("select");
      try {
        // 捕获指针:拖出窗口再松开也能收到 pointerup,选区状态不卡死
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        // 环境不支持时忽略,窗口内拖选不受影响
      }
    },
    [mode, sel],
  );

  const onSelectPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (!selectDraggingRef.current || !selStartRef.current) return;
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
    selectDraggingRef.current = false;
    if (!sel || sel.w < 2 || sel.h < 2) {
      selStartRef.current = null;
      selEndRef.current = null;
      setSel(null);
      return;
    }
    setMode("confirm");
  }, [sel]);

  // 系统取消指针(切窗口/触控手势)时终止拖选,避免残留在"跟鼠标"状态
  const onSelectPointerCancel = useCallback(() => {
    selectDraggingRef.current = false;
    selStartRef.current = null;
    selEndRef.current = null;
    setSel(null);
  }, []);

  const cancel = useCallback(() => {
    onCancel();
  }, [onCancel]);

  const confirm = useCallback(() => {
    const source: HTMLVideoElement | HTMLImageElement | null = imageSrc
      ? imgRef.current
      : videoRef.current;
    const native = nativeSelection();
    if (!source || !native || native.w < 2 || native.h < 2) {
      setError("empty");
      return;
    }
    void (async () => {
      const { w: vw, h: vh } = sourceSize();
      if (!vw || !vh) {
        setError("empty");
        return;
      }
      const out = document.createElement("canvas");
      out.width = Math.round(native.w);
      out.height = Math.round(native.h);
      const ctx = out.getContext("2d");
      if (!ctx) return;
      ctx.drawImage(source, native.x, native.y, native.w, native.h, 0, 0, out.width, out.height);
      const blob = await canvasToBlob(out);
      if (!blob) {
        setError("empty");
        return;
      }
      onComplete(new File([blob], `screenshot-${Date.now()}.png`, { type: "image/png" }));
    })();
  }, [imageSrc, nativeSelection, onComplete, sourceSize]);

  // Esc 取消
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        cancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cancel]);

  return createPortal(
    <div
      ref={containerRef}
      className="fixed inset-0 z-[100] select-none"
      role="dialog"
      aria-modal="true"
      aria-label={t("thread.composer.screenshot.dialogAria")}
    >
      {/* 实时画面 / 后端截图 + 调暗 / 亮区 */}
      <div
        onPointerDown={onSelectPointerDown}
        onPointerMove={onSelectPointerMove}
        onPointerUp={onSelectPointerUp}
        onPointerCancel={onSelectPointerCancel}
        className="absolute inset-0 cursor-crosshair touch-none overflow-hidden bg-black"
      >
        {imageSrc ? (
          <img
            ref={imgRef}
            src={imageSrc}
            alt=""
            draggable={false}
            onLoad={() => setReady(true)}
            className="block h-full w-full select-none object-contain"
          />
        ) : (
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            className="h-full w-full object-contain"
          />
        )}
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
          {!imageSrc ? (
            <p className="rounded-full bg-black/40 px-3 py-1 text-[11.5px] text-white/70 backdrop-blur">
              {t("thread.composer.screenshot.sharingHint")}
            </p>
          ) : null}
        </div>
      ) : null}

      {/* 确认工具栏 */}
      {mode === "confirm" && sel ? (
        <div
          className="absolute z-20 flex items-center gap-1 rounded-xl border border-white/15 bg-black/80 p-1 backdrop-blur"
          style={toolbarStyle(sel, containerRef.current)}
        >
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
