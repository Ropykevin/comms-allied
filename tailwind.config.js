/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/templates/**/*.html", "./app/static/js/**/*.js", "./app/**/*.py"],
  safelist: [
    // Category colours are chosen by users at runtime.
    { pattern: /(bg|text|ring|border)-(indigo|emerald|amber|sky|rose|violet|teal|orange|slate|lime)-(50|100|500|600|700)/ },
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#eef6fb",
          100: "#d5e9f5",
          200: "#aed3ea",
          300: "#79b4d9",
          400: "#4791c2",
          500: "#2774a8",
          600: "#1b5c8c",
          700: "#174a72",
          800: "#163f5f",
          900: "#0f2a42",
          950: "#0a1c2e",
        },
        sun: {
          50: "#fff8eb",
          100: "#feebc7",
          200: "#fdd48a",
          300: "#fcb84d",
          400: "#fb9d24",
          500: "#f07a0b",
          600: "#d45806",
          700: "#b03b09",
        },
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "-apple-system", "Roboto", "Helvetica Neue", "Arial", "sans-serif"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(15, 42, 66, 0.04), 0 4px 16px rgba(15, 42, 66, 0.06)",
      },
    },
  },
  plugins: [require("@tailwindcss/forms")],
};
