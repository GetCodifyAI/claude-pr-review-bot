// Nav icons, ported from the server's NAV_ICONS.
const svg = (children: React.ReactNode) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
       strokeLinecap="round" strokeLinejoin="round">{children}</svg>
);

export const NavIcon: Record<string, React.ReactNode> = {
  queue: svg(<path d="M3 5h18M3 12h18M3 19h11" />),
  qa: svg(<><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></>),
  learnings: svg(<path d="M12 3l2.1 4.5L19 8.2l-3.5 3.3.9 4.9L12 14.1 7.6 16.4l.9-4.9L5 8.2l4.9-.7z" />),
  skills: svg(<path d="M16 18l6-6-6-6M8 6l-6 6 6 6" />),
  integrations: svg(<>
    <rect x="3" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="14" width="7" height="7" rx="1.5" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" />
  </>),
  how: svg(<><circle cx="12" cy="12" r="9" /><path d="M9.6 9.2a2.5 2.5 0 1 1 3.4 2.3c-.8.5-1 .9-1 1.7M12 17h.01" /></>),
};

// Brand icons for the Integrations page (GitHub / Slack / Claude).
export const BrandIcon = {
  gh: (
    <svg viewBox="0 0 16 16" width={23} height={23} fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  ),
  slack: (
    <svg viewBox="0 0 122.8 122.8" width={22} height={22} aria-hidden="true">
      <path fill="#E01E5A" d="M25.8 77.6a12.9 12.9 0 1 1-12.9-12.9h12.9zM32.3 77.6a12.9 12.9 0 0 1 25.8 0v32.3a12.9 12.9 0 0 1-25.8 0z" />
      <path fill="#36C5F0" d="M45.2 25.8a12.9 12.9 0 1 1 12.9-12.9v12.9zM45.2 32.3a12.9 12.9 0 0 1 0 25.8H12.9a12.9 12.9 0 0 1 0-25.8z" />
      <path fill="#2EB67D" d="M97 45.2a12.9 12.9 0 1 1 12.9 12.9H97zM90.5 45.2a12.9 12.9 0 0 1-25.8 0V12.9a12.9 12.9 0 0 1 25.8 0z" />
      <path fill="#ECB22E" d="M77.6 97a12.9 12.9 0 1 1-12.9 12.9V97zM77.6 90.5a12.9 12.9 0 0 1 0-25.8h32.3a12.9 12.9 0 0 1 0 25.8z" />
    </svg>
  ),
  claude: (
    <svg viewBox="0 0 24 24" width={18} height={18} fill="currentColor" aria-hidden="true">
      <path d="M12 2c.3 3.1 1 4.9 2.2 6 1.1 1.2 2.9 1.9 6 2.2-3.1.3-4.9 1-6 2.2-1.2 1.1-1.9 2.9-2.2 6-.3-3.1-1-4.9-2.2-6C8.6 11.2 6.8 10.5 3.7 10.2c3.1-.3 4.9-1 6-2.2C10.9 6.9 11.6 5.1 12 2z" />
    </svg>
  ),
};
