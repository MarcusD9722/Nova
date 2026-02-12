/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./index.html",
    "./src/**/*.{js,jsx,ts,tsx}",
    "./*.{js,jsx,ts,tsx}"
  ],
  theme: {
    extend: {
      colors: {
        'nova-purple': '#7C3AED',
        'nova-gold': '#FFD700',
        'glass': 'rgba(32,32,64,0.60)'
      },
    },
  },
  plugins: [],
};
