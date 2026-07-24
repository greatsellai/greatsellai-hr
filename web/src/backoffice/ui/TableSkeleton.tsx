export function TableSkeleton() {
  return (
    <div className="empty-state" aria-busy="true" aria-live="polite">
      <div className="empty-state-inner">
        <div
          className="skeleton"
          style={{ width: "3.75rem", height: "3.75rem", borderRadius: "50%" }}
        />
        <div className="skeleton" style={{ width: "13rem", height: "1rem" }} />
        <div
          className="skeleton"
          style={{ width: "18rem", height: "0.875rem" }}
        />
      </div>
    </div>
  );
}
