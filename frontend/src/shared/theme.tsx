import * as React from 'react';
import { MoonIcon, SunIcon } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { readString, THEME_KEY, writeString } from '@/lib/storage';

type Theme = 'light' | 'dark';

function systemTheme(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function storedTheme(): Theme | null {
  const value = readString(THEME_KEY);
  return value === 'light' || value === 'dark' ? value : null;
}

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle('dark', theme === 'dark');
}

const ThemeContext = React.createContext<{ theme: Theme; toggle: () => void }>({
  theme: 'light',
  toggle: () => {},
});

/** Follows the system setting until the visitor picks a theme, which is then remembered here. */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = React.useState<Theme>(() => storedTheme() ?? systemTheme());

  React.useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  React.useEffect(() => {
    const query = window.matchMedia?.('(prefers-color-scheme: dark)');
    if (!query) return;
    const onChange = () => {
      if (!storedTheme()) setTheme(systemTheme());
    };
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  const toggle = React.useCallback(() => {
    setTheme((current) => {
      const next = current === 'dark' ? 'light' : 'dark';
      writeString(THEME_KEY, next);
      return next;
    });
  }, []);

  const value = React.useMemo(() => ({ theme, toggle }), [theme, toggle]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  return React.useContext(ThemeContext);
}

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const label = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
  return (
    <Button variant="ghost" size="icon" onClick={toggle} aria-label={label} title={label}>
      {theme === 'dark' ? <SunIcon className="size-[18px]" /> : <MoonIcon className="size-[18px]" />}
    </Button>
  );
}
