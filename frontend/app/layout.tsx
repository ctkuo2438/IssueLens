import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "IssueLens · A clearer view of every issue",
  description:
    "Investigate GitHub issues with an evidence-grounded AI assistant.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
