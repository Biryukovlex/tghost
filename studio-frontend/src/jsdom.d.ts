declare module "jsdom" {
  export interface JSDOMOptions {
    url?: string;
    runScripts?: "outside-only" | "dangerously";
    pretendToBeVisual?: boolean;
  }

  export class JSDOM {
    constructor(html?: string, options?: JSDOMOptions);
    window: Window & typeof globalThis & {
      close: () => void;
    };
  }
}
