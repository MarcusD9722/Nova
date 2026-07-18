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
        'nova-purple': '#8B5CF6',
        'nova-gold': '#F5C542',
        // Purple-tinted glass (no opacity on container; text stays 100% opaque)
        'glass': 'rgba(18,12,40,0.62)',
        'nova-bg': '#060217'
      },
      fontFamily: {
        orbitron: ['Orbitron', 'sans-serif'],
        rajdhani: ['Rajdhani', 'sans-serif'],
        space: ['Space Grotesk', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
