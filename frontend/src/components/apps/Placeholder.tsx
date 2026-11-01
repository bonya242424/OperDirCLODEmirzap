export default function Placeholder({ title, stage }: { title: string; stage: number }) {
  return (
    <div className="placeholder">
      <h2>{title}</h2>
      <p className="muted">Раздел в разработке{stage ? `: этап ${stage} из плана в ТЗ` : ""}.</p>
    </div>
  );
}
