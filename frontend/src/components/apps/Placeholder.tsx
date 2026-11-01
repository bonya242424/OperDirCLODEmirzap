import { PLANS } from "../../sections";

export default function Placeholder({ appKey, title, stage }: { appKey: string; title: string; stage: number }) {
  const plan = PLANS[appKey];
  return (
    <div className="placeholder plan">
      <h2>{title}</h2>
      <p className="muted">Раздел в разработке{stage ? `, этап ${stage} плана` : ""}.</p>
      {plan && (
        <>
          <p className="plan-purpose">{plan.purpose}</p>
          <h4>Что здесь будет</h4>
          <ul>
            {plan.items.map((i) => (
              <li key={i}>{i}</li>
            ))}
          </ul>
          {plan.note && <p className="muted small">{plan.note}</p>}
        </>
      )}
    </div>
  );
}
