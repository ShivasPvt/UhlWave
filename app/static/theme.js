// Shared colours and fonts for Tailwind. Change the palette here and both pages follow.
tailwind.config = {
  theme: {
    extend: {
      colors: {
        pine: { DEFAULT: "#1E3A34", 600: "#2C5048", 400: "#5E7E76" },
        mist: "#F1F4F2",
        glacier: "#DCE9E6",
        slate: "#51626A",
        ink: "#17201E",
        marigold: { DEFAULT: "#E9A23B", soft: "#FBEBD0", deep: "#8A5A12" },
      },
      fontFamily: {
        display: ['"Bricolage Grotesque"', "system-ui", "sans-serif"],
        body: ['"Atkinson Hyperlegible"', "system-ui", "sans-serif"],
      },
    },
  },
};
