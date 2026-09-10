import { useEffect, useMemo, useState } from "react";

import { Check, ChevronDown, Sparkles } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { resolveCustomBrand, inferProviderFromModelName, type ProviderBrand } from "@/lib/provider-brand";
import { cn } from "@/lib/utils";

export interface ComposerModelBadgeProps {
  label: string;
  provider?: string | null;
  providerLabel?: string | null;
  /** 当前 provider 的 api_base(用于 custom 动态 brand 图标生成)。 */
  apiBase?: string | null;
  /** 当前 provider 下可用模型列表。当 models.length > 1 时启用下拉选择,
   * 否则保持静态徽章展示(行为同旧版)。 */
  models?: string[];
  isHero: boolean;
  /** 用户在弹出菜单中选择其他模型时触发。 */
  onSelect?: (model: string) => void;
}

/** 输入框右下角的模型徽章,展示模型名 + provider 图标(logo 失败时回退首字母)。
 * 当传入多个 models 且 onSelect 可用时,变为可点击的下拉选择器。 */
export function ComposerModelBadge({
  label,
  provider,
  providerLabel,
  apiBase,
  models,
  isHero,
  onSelect,
}: ComposerModelBadgeProps) {
  const inferredProvider = provider || inferProviderFromModelName(label);
  // custom(单例或虚拟 row custom__xxx):用 api_base 动态生成 brand;
  // 其余用内置 providerBrand
  const brand: ProviderBrand | null = useMemo(
    () => resolveCustomBrand(inferredProvider, apiBase),
    [inferredProvider, apiBase],
  );
  const [logoIndex, setLogoIndex] = useState(0);
  const logoUrl = brand?.logoUrls[logoIndex];
  const showLogo = !!logoUrl;
  const title = providerLabel ? `${label} · ${providerLabel}` : label;

  useEffect(() => setLogoIndex(0), [inferredProvider, apiBase]);

  // 公共 logo 渲染(触发器与静态徽章共用)
  const logoSlot = (
    <span
      data-testid={inferredProvider ? `composer-model-logo-${inferredProvider}` : "composer-model-logo"}
      className={cn(
        "grid shrink-0 place-items-center overflow-hidden rounded-full border bg-background",
        isHero ? "h-4 w-4" : "h-5 w-5",
      )}
      style={{
        borderColor: brand ? `${brand.color}28` : undefined,
        boxShadow: brand ? `inset 0 0 0 1px ${brand.color}18` : undefined,
      }}
      aria-hidden
    >
      {showLogo ? (
        <img
          src={logoUrl}
          alt=""
          className={cn("object-contain", isHero ? "h-3 w-3" : "h-3.5 w-3.5")}
          onError={() => setLogoIndex((index) => index + 1)}
        />
      ) : brand ? (
        <span
          className={cn(
            "grid h-full w-full place-items-center rounded-full text-white",
            isHero ? "text-[7.5px]" : "text-[8px]",
          )}
          style={{ backgroundColor: brand.color }}
        >
          {brand.initials.slice(0, 2)}
        </span>
      ) : (
        <Sparkles className={cn("text-muted-foreground/65", isHero ? "h-3 w-3" : "h-3 w-3")} />
      )}
    </span>
  );

  const containerClass = cn(
    "inline-flex min-w-0 items-center rounded-full border border-border/55 bg-card font-medium text-foreground/82",
    "shadow-[0_2px_8px_rgba(15,23,42,0.045)]",
    "focus-visible:outline-none focus-visible:ring-0",
    isHero ? "h-8 max-w-[12.5rem] gap-1.5 px-2 text-[11.5px]" : "h-9 max-w-[12rem] gap-2 px-2.5 text-[12px]",
  );

  // 没有可选模型列表或只有 1 个模型:保持静态徽章
  const selectableModels = (models ?? []).filter((m) => m && m.trim().length > 0);
  if (!onSelect || selectableModels.length <= 1) {
    return (
      <span title={title} className={containerClass}>
        {logoSlot}
        <span className="truncate">{label}</span>
      </span>
    );
  }

  // 多模型:启用下拉选择
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          title={title}
          className={cn(containerClass, "transition-colors hover:bg-accent/40 hover:text-foreground")}
        >
          {logoSlot}
          <span className="truncate">{label}</span>
          <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[18rem] w-[260px] max-w-[calc(100vw-1rem)] overflow-y-auto"
      >
        <DropdownMenuLabel className="text-[11px] uppercase tracking-wide text-muted-foreground">
          {providerLabel ?? inferredProvider ?? "Models"}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {selectableModels.map((model) => {
          const selected = model === label;
          const leaf = model.split("/").pop() ?? model;
          return (
            <DropdownMenuItem
              key={model}
              onSelect={() => onSelect(model)}
              className={cn(
                "flex items-center justify-between gap-2 rounded-[10px] px-2.5 py-2 text-[12.5px]",
                "focus:bg-muted/85 focus:text-foreground",
                selected && "bg-muted/80 text-foreground focus:bg-muted",
              )}
            >
              <span className="min-w-0 flex-1 truncate" title={model}>{leaf}</span>
              {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
