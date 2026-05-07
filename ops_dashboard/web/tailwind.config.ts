import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        // Iridium terminal palette: near-black dark, parchment fg,
        // single warm-gold accent, signal red/green for trust deltas.
        ink: {
          900: "#0B0B0E",
          800: "#101015",
          700: "#15151B",
          600: "#1C1C24",
          500: "#26262E",
          400: "#3A3A45",
        },
        parchment: {
          50: "#F4F1E9",
          100: "#E8E4DA",
          200: "#C9C4B6",
          300: "#9B968A",
          400: "#787581",
        },
        accent: {
          500: "#D4A24C",
          400: "#E5B868",
          600: "#A07A35",
        },
        signal: {
          green: "#5DBB7A",
          red: "#E04848",
          blue: "#5478A6",
        },
      },
      fontFamily: {
        display: ["var(--font-display)", "Georgia", "serif"],
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      letterSpacing: {
        tightest: "-0.04em",
        tighter: "-0.02em",
        wide: "0.04em",
        wider: "0.08em",
        widest: "0.16em",
      },
      boxShadow: {
        "inner-line":
          "inset 0 0 0 1px rgba(232, 228, 218, 0.06)",
      },
      animation: {
        "fade-up": "fadeUp 0.6s cubic-bezier(0.16, 1, 0.3, 1) both",
        "ticker": "ticker 1.2s ease-out both",
      },
      keyframes: {
        fadeUp: {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        ticker: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
      },
    },
  },
  plugins: [],
};

export default config;
