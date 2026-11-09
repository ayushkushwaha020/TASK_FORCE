interface ImportMetaEnv { readonly VITE_API_BASE_URL?: string }
interface ImportMeta { readonly env: ImportMetaEnv }


declare module "*.b64?raw" {
  const content: string;
  export default content;
}
