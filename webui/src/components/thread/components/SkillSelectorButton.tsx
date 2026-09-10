import { Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { SkillInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface SkillSelectorButtonProps {
  /** 已过滤好的可选 skill 列表（通常为 available && !disabled）。 */
  skills: SkillInfo[];
  disabled?: boolean;
  isHero: boolean;
  onSelect: (skillName: string) => void;
  ariaLabel: string;
  emptyLabel: string;
  builtinBadge: string;
  workspaceBadge: string;
}

/** 输入框左下角的 skill 选择按钮(Sparkles 图标),点击展开下拉菜单。
 *  菜单风格与应用标准下拉(模型选择/MCP 传输方式)一致:单行项 +
 *  muted 图标 + 中性徽章;完整描述经 title 提示展示。
 *  - 选中 skill 后调用 ``onSelect(skill.name)``,由主组件把提示文字插入输入框,
 *    引导 LLM 主动 ``read_file`` 该 skill 的 SKILL.md 并按其指示工作。 */
export function SkillSelectorButton({
  skills,
  disabled,
  isHero,
  onSelect,
  ariaLabel,
  emptyLabel,
  builtinBadge,
  workspaceBadge,
}: SkillSelectorButtonProps) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          disabled={disabled}
          aria-label={ariaLabel}
          title={ariaLabel}
          className={cn(
            "rounded-full transition-colors",
            "focus-visible:ring-0 focus-visible:ring-offset-0",
            isHero
              ? "h-8 w-8 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card"
              : "h-9 w-9 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
            "text-muted-foreground hover:text-foreground",
          )}
        >
          <Sparkles className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className={cn(
          // 容器对齐页面弹窗(Dialog)的卡片语言:实色背景、rounded-lg、
          // border-border/60、shadow-lg;覆盖默认下拉的毛玻璃样式
          // (bg-popover/96 因 tailwind 配色缺 <alpha-value> 实际未生成)
          "rounded-lg border-border/60 bg-background backdrop-blur-none shadow-lg dark:border-border/60 dark:shadow-lg",
          "max-h-[18rem] w-[300px] max-w-[calc(100vw-1rem)] overflow-y-auto scrollbar-none",
        )}
      >
        <DropdownMenuLabel className="text-[11px] uppercase tracking-wide text-muted-foreground">
          {ariaLabel}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {skills.length === 0 ? (
          <div className="px-2.5 py-2 text-[12.5px] text-muted-foreground">
            {emptyLabel}
          </div>
        ) : (
          skills.map((skill) => (
            <DropdownMenuItem
              key={skill.name}
              onSelect={() => onSelect(skill.name)}
              title={skill.description ?? skill.name}
              className="text-[12.5px]"
            >
              <Sparkles className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <span className="min-w-0 flex-1 truncate">{skill.name}</span>
              <span className="ml-1 shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                {skill.source === "builtin" ? builtinBadge : workspaceBadge}
              </span>
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
