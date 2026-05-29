"use strict";

module.exports = async function (context, _req) {
  context.res = {
    status: 200,
    headers: { "Content-Type": "application/json" },
    body: {
      status: "ok",
      app: process.env.WEBSITE_SITE_NAME || "unknown",
      node: process.version,
      time: new Date().toISOString(),
    },
  };
};
