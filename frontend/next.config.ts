import type { NextConfig } from "next";

const config: NextConfig = {
  poweredByHeader: false,
  logging: {
    incomingRequests: {
      ignore: [/\/api\/v1\/auth\/github\/callback(?:[/?]|$)/],
    },
  },
};

export default config;
