import { AtSign, Check, Users, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { AgentInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface AgentSelectorButtonProps {
  agents: AgentInfo[];
  selectedAgentId: string | null;
  disabled?: boolean;
  isHero: boolean;
  onSelect: (agentId: string) => void;
  ariaLabel: string;
  emptyLabel: string;
  clearLabel: string;
}

/** 输入框左下角的 @ subagent 选择按钮(AtSign 图标),点击展开下拉菜单。
 *  常驻显示:无 agent 时菜单展示创建引导,避免入口不可见。
 *  菜单风格与应用标准下拉(模型选择/MCP 传输方式)一致:单行项 +
 *  muted 图标 + Check 标记选中;完整描述经 title 提示展示。
 *  - 选择 agent 时调用 ``onSelect(agent.name)``。
 *  - 清除时调用 ``onSelect("__none__")``,由主组件转译为 ``onClearAgent``。 */
export function AgentSelectorButton({
  agents,
  selectedAgentId,
  disabled,
  isHero,
  onSelect,
  ariaLabel,
  emptyLabel,
  clearLabel,
}: AgentSelectorButtonProps) {
  const active = !!selectedAgentId;
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
            "rounded-full transition-colors font-semibold",
            // 完全移除焦点环:菜单 Esc 关闭后焦点回到按钮,环在圆形图标
            // 按钮上只有视觉噪音,键盘用户仍有 title/aria-label 可依
            "focus-visible:ring-0 focus-visible:ring-offset-0",
            isHero
              ? "h-8 w-8 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card"
              : "h-9 w-9 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
            active
              ? "text-sky-600 hover:text-sky-600 dark:text-sky-400"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <AtSign className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className={cn(
          // 容器对齐页面弹窗(Dialog)的卡片语言:实色背景、rounded-lg、
          // border-border/60、shadow-lg;覆盖默认下拉的毛玻璃样式
          // (bg-popover/96 因 tailwind 配色缺 <alpha-value> 实际未生成,
          // 默认下拉一直是透明+blur,与页面弹窗风格割裂)
          "rounded-lg border-border/60 bg-background backdrop-blur-none shadow-lg dark:border-border/60 dark:shadow-lg",
          "max-h-[18rem] w-[280px] max-w-[calc(100vw-1rem)] overflow-y-auto scrollbar-none",
        )}
      >
        <DropdownMenuLabel className="text-[11px] uppercase tracking-wide text-muted-foreground">
          {ariaLabel}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {agents.length === 0 ? (
          <div className="px-2.5 py-2 text-[12.5px] text-muted-foreground">
            {emptyLabel}
          </div>
        ) : (
          agents.map((agent) => {
            const selected = agent.name === selectedAgentId;
            return (
              <DropdownMenuItem
                key={agent.name}
                onSelect={() => onSelect(agent.name)}
                title={agent.description ?? agent.name}
                className="text-[12.5px]"
              >
                <Users className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                <span className="min-w-0 flex-1 truncate">{agent.name}</span>
                {selected ? (
                  <Check className="ml-auto h-3.5 w-3.5 shrink-0" aria-hidden />
                ) : null}
              </DropdownMenuItem>
            );
          })
        )}
        {selectedAgentId ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={() => onSelect("__none__")}
              className="text-[12.5px] text-muted-foreground"
            >
              <X className="h-3.5 w-3.5 shrink-0" aria-hidden />
              {clearLabel}
            </DropdownMenuItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
