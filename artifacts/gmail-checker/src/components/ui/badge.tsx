import * as React from "react"
import { cn } from "@/lib/utils"

export interface BadgeProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: "default" | "secondary" | "destructive" | "outline" | "valid" | "invalid" | "catchall" | "unknown"
}

function Badge({ className, variant = "default", ...props }: BadgeProps) {
  return (
    <div
      className={cn(
        "inline-flex items-center rounded-md border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
        {
          "border-transparent bg-primary text-primary-foreground shadow hover:bg-primary/80": variant === "default",
          "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80": variant === "secondary",
          "border-transparent bg-destructive text-destructive-foreground shadow hover:bg-destructive/80": variant === "destructive",
          "text-foreground": variant === "outline",
          "border-transparent bg-valid/20 text-valid border-valid/50 border": variant === "valid",
          "border-transparent bg-invalid/20 text-invalid border-invalid/50 border": variant === "invalid",
          "border-transparent bg-catchall/20 text-catchall border-catchall/50 border": variant === "catchall",
          "border-transparent bg-unknown/20 text-unknown border-unknown/50 border": variant === "unknown",
        },
        className
      )}
      {...props}
    />
  )
}

export { Badge }
