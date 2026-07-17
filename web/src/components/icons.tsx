// Minimal 16px stroke icon set (lucide-style paths), so the nav doesn't rely
// on unicode glyphs.
type IconProps = { className?: string }

function base(props: IconProps, children: React.ReactNode) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={props.className ?? 'h-4 w-4'}
      aria-hidden
    >
      {children}
    </svg>
  )
}

export const IconOverview = (p: IconProps) =>
  base(p, <><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></>)

export const IconBills = (p: IconProps) =>
  base(p, <><path d="M6 3h12a1 1 0 0 1 1 1v16l-2.5-1.5L14 20l-2-1.5L10 20l-2.5-1.5L5 20V4a1 1 0 0 1 1-1Z" /><path d="M9 8h6M9 12h6" /></>)

export const IconReports = (p: IconProps) =>
  base(p, <><path d="M4 20V10M10 20V4M16 20v-7M21 20H3.5" /></>)

export const IconProperties = (p: IconProps) =>
  base(p, <><path d="M4 21V5a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v16" /><path d="M14 9h5a1 1 0 0 1 1 1v11" /><path d="M2 21h20" /><path d="M8 8h2M8 12h2M8 16h2M17 13h1M17 17h1" /></>)

export const IconAccounts = (p: IconProps) =>
  base(p, <><path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12L13 2Z" /></>)

export const IconCapture = (p: IconProps) =>
  base(p, <><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" /></>)

export const IconIntegrations = (p: IconProps) =>
  base(p, <><rect x="3" y="3" width="8" height="8" rx="1.5" /><rect x="13" y="13" width="8" height="8" rx="1.5" /><path d="M17 3v6M14 6h6M7 13v8M3 17h8" /></>)

export const IconSettings = (p: IconProps) =>
  base(p, <><path d="M4 21v-6M4 9V3M12 21v-9M12 6V3M20 21v-4M20 11V3" /><path d="M2 15h4M10 8h4M18 17h4" /></>)
