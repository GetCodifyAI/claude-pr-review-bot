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
