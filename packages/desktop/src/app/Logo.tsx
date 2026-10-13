export function Logo({ size = 40, className }: { size?: number; className?: string }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      aria-hidden="true"
    >
      <rect
        x="6.5"
        y="6.5"
        width="35"
        height="35"
        rx="13"
        stroke="currentColor"
        strokeWidth="2.6"
      />
      <path d="M17 24.5h14" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" />
    </svg>
  );
}
