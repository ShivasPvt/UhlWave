// Shared colours and fonts for Tailwind. Change the palette here and both pages follow.
// The original names (pine, mist, marigold…) are kept so chat.js keeps working;
// their values are now a brighter violet / berry / sunshine palette.
tailwind.config = {
  theme: {
    extend: {
      colors: {
        pine: { DEFAULT: "#2B1867", 600: "#3F2591", 400: "#9A88E0" }, // deep violet (sidebar, buttons)
        mist: "#FFF8F2",      // warm cream page background
        glacier: "#EEE7FF",   // soft lavender text on dark
        slate: "#605A78",
        ink: "#1C1433",
        marigold: { DEFAULT: "#FFB627", soft: "#FFF1CC", deep: "#8A5300" }, // the AI's colour
        grape: "#7C3AED",
        berry: "#FF3D81",
        tangerine: "#FF8A3D",
        sky: "#22C3EE",
        lime: "#8BE04E",
      },
      fontFamily: {
        display: ['"Bricolage Grotesque"', "system-ui", "sans-serif"],
        body: ['"Atkinson Hyperlegible"', "system-ui", "sans-serif"],
      },
      keyframes: {
        drift: {
          "0%, 100%": { transform: "translate(0, 0) scale(1)" },
          "50%": { transform: "translate(30px, -40px) scale(1.12)" },
        },
        shimmer: {
          "0%, 100%": { backgroundPosition: "0% 50%" },
          "50%": { backgroundPosition: "100% 50%" },
        },
        pop: {
          "0%": { opacity: "0", transform: "translateY(12px) scale(.96)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
      },
      animation: {
        drift: "drift 14s ease-in-out infinite",
        "drift-slow": "drift 20s ease-in-out infinite reverse",
        shimmer: "shimmer 12s ease infinite",
        pop: "pop .5s cubic-bezier(.2,.8,.2,1) both",
      },
    },
  },
};
