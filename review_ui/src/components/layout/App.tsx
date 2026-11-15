import { Link, Outlet } from '@tanstack/react-router';

export default function App() {
  return (
    <>
      <header className="app-header flex h-[65px] items-center justify-between border-b border-border bg-card px-5 min-[661px]:h-[76px] min-[661px]:px-[4%]">
        <Link className="workspace-name text-xl font-semibold tracking-[-.6px] text-foreground no-underline focus-visible:outline-2 focus-visible:outline-ring min-[661px]:text-[22px]" to="/clients" search={{}}>
          Review workspace
        </Link>
        <span className="local-indicator flex items-center gap-2 text-[8px] font-semibold tracking-[.8px] text-muted-foreground min-[661px]:text-[9px] min-[661px]:tracking-[1.3px]">
          <span className="size-1.5 rounded-full bg-[#529270]" />
          LOCAL WORKSPACE
        </span>
      </header>
      <Outlet />
      <footer className="app-footer mx-5 flex flex-col gap-1.5 border-t border-border py-[25px] text-[9px] text-muted-foreground min-[661px]:mx-[4%] min-[661px]:flex-row min-[661px]:justify-between min-[661px]:gap-5 min-[661px]:text-[10px]">
        <span>Review workspace · Local storage</span>
        <span>Review decisions and source history stay in this workspace.</span>
      </footer>
    </>
  );
}
