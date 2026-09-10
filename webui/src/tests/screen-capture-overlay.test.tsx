import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ScreenCaptureOverlay } from "@/components/thread/components/ScreenCaptureOverlay";

function selectSurface(dialog: HTMLElement): HTMLElement {
  const el = dialog.querySelector(".cursor-crosshair");
  if (!el) throw new Error("select surface not found");
  return el as HTMLElement;
}

function selectionBox(dialog: HTMLElement): HTMLElement {
  const el = dialog.querySelector(".border-2");
  if (!el) throw new Error("selection box not found");
  return el as HTMLElement;
}

function drag(
  surface: HTMLElement,
  from: [number, number],
  to: [number, number],
) {
  fireEvent.pointerDown(surface, {
    button: 0,
    pointerId: 1,
    clientX: from[0],
    clientY: from[1],
    buttons: 1,
  });
  fireEvent.pointerMove(surface, {
    pointerId: 1,
    clientX: to[0],
    clientY: to[1],
    buttons: 1,
  });
  fireEvent.pointerUp(surface, {
    pointerId: 1,
    clientX: to[0],
    clientY: to[1],
    buttons: 0,
  });
}

function renderOverlay() {
  render(
    <ScreenCaptureOverlay
      imageSrc="blob:shot"
      onComplete={vi.fn()}
      onCancel={vi.fn()}
    />,
  );
  const dialog = screen.getByRole("dialog");
  fireEvent.load(dialog.querySelector("img") as HTMLImageElement);
  return dialog;
}

describe("ScreenCaptureOverlay", () => {
  it("freezes the selection after release and shows the confirm toolbar", async () => {
    const dialog = renderOverlay();

    const surface = selectSurface(dialog);
    drag(surface, [100, 100], [300, 260]);

    expect(
      await screen.findByRole("button", { name: "Send" }),
    ).toBeInTheDocument();

    const box = selectionBox(dialog);
    expect(box.style.left).toBe("100px");
    expect(box.style.width).toBe("200px");

    // 回归:松开后的普通鼠标移动不得改写选区(选区曾跟着鼠标漂移,
    // 工具栏随之漂移,确认按钮无法点击)
    fireEvent.pointerMove(surface, {
      pointerId: 1,
      clientX: 500,
      clientY: 500,
      buttons: 0,
    });
    const boxAfter = selectionBox(dialog);
    expect(boxAfter.style.width).toBe("200px");
    expect(
      screen.getByRole("button", { name: "Send" }),
    ).toBeInTheDocument();
  });

  it("keeps the selection when clicking inside the confirmed region", async () => {
    const dialog = renderOverlay();

    const surface = selectSurface(dialog);
    drag(surface, [100, 100], [400, 300]);
    expect(
      await screen.findByRole("button", { name: "Send" }),
    ).toBeInTheDocument();

    // 回归:选区内点击不得重置选区(标注 canvas 移除后,选区内点击
    // 直接落在拖选表面上,若不防护会清空选区)
    fireEvent.pointerDown(surface, {
      button: 0,
      pointerId: 2,
      clientX: 150,
      clientY: 150,
      buttons: 1,
    });
    fireEvent.pointerUp(surface, {
      pointerId: 2,
      clientX: 150,
      clientY: 150,
      buttons: 0,
    });

    expect(
      screen.getByRole("button", { name: "Send" }),
    ).toBeInTheDocument();
    expect(selectionBox(dialog).style.width).toBe("300px");
  });

  it("shows only cancel and send buttons in the toolbar", async () => {
    const dialog = renderOverlay();

    const surface = selectSurface(dialog);
    drag(surface, [100, 100], [400, 300]);
    const toolbar = await screen.findByRole("button", { name: "Send" });

    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    // 标注工具(矩形/椭圆/箭头/画笔/文字/撤销)已按需求移除
    expect(toolbar.parentElement?.querySelectorAll("button")).toHaveLength(2);
  });

  it("restarts selection when clicking outside the confirmed region", async () => {
    const dialog = renderOverlay();

    const surface = selectSurface(dialog);
    drag(surface, [100, 100], [400, 300]);
    expect(
      await screen.findByRole("button", { name: "Send" }),
    ).toBeInTheDocument();

    // 选区之外(暗区)点击 = 放弃当前选区,回到拖选模式
    fireEvent.pointerDown(surface, {
      button: 0,
      pointerId: 3,
      clientX: 600,
      clientY: 500,
      buttons: 1,
    });
    fireEvent.pointerUp(surface, {
      pointerId: 3,
      clientX: 600,
      clientY: 500,
      buttons: 0,
    });

    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
  });
});
