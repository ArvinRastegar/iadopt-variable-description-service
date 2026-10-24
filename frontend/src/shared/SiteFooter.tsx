import logoUrl from '../../logo.svg';

/** Credits, carried over from the previous footer. */
export function SiteFooter() {
  return (
    <footer className="border-t border-border px-5 pt-12 pb-10" aria-labelledby="credits-heading">
      <div className="mx-auto flex max-w-[1120px] flex-col gap-9">
        <h2 id="credits-heading" className="sr-only">Acknowledgements and contributors</h2>
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(240px,100%),1fr))] gap-8">
          <div className="flex flex-col gap-3">
            <img src={logoUrl} alt="I-ADOPT" className="block h-10 w-auto self-start" />
            <p className="m-0 text-[13px] leading-[19px] text-text-2">
              Variable Description Service, built by the GO FAIR Foundation and the Karlsruhe Institute of Technology (KIT).
            </p>
            <div className="flex flex-wrap gap-3.5 text-[13px] font-medium">
              <a href="https://w3id.org/iadopt" target="_blank" rel="noopener noreferrer">I-ADOPT framework</a>
            </div>
          </div>
          <div className="flex flex-col gap-2.5">
            <h3 className="m-0 text-xs font-semibold tracking-[0.05em] text-muted-foreground uppercase">Technical lead and main developer</h3>
            <p className="m-0 text-[13px] leading-5">
              <strong className="font-semibold">Arvin Rastegar</strong>
              <br />
              <span className="text-text-2">GO FAIR Foundation; Karlsruhe Institute of Technology (KIT)</span>
              <br />
              <a href="mailto:arvin.rastegar@kit.edu">arvin.rastegar@kit.edu</a>
              <br />
              <a href="mailto:arvin@gofair.foundation">arvin@gofair.foundation</a>
            </p>
          </div>
          <div className="flex flex-col gap-2.5">
            <h3 className="m-0 text-xs font-semibold tracking-[0.05em] text-muted-foreground uppercase">Contact persons</h3>
            <p className="m-0 text-[13px] leading-5">
              <strong className="font-semibold">Barbara Magagna</strong>
              <br />
              <span className="text-text-2">GO FAIR Foundation</span>
              <br />
              <a href="mailto:barbara@gofair.foundation">barbara@gofair.foundation</a>
            </p>
            <p className="m-0 text-[13px] leading-5">
              <strong className="font-semibold">Christof Lorenz</strong>
              <br />
              <span className="text-text-2">Karlsruhe Institute of Technology (KIT)</span>
              <br />
              <a href="mailto:christof.lorenz@kit.edu">christof.lorenz@kit.edu</a>
            </p>
          </div>
          <div className="flex flex-col gap-2.5">
            <h3 className="m-0 text-xs font-semibold tracking-[0.05em] text-muted-foreground uppercase">With thanks to</h3>
            <p className="m-0 text-[13px] leading-5 text-text-2">We warmly thank the following people for their generous support of this project:</p>
            <ul className="m-0 flex list-none flex-col gap-2 p-0 text-[13px] leading-5">
              <li><strong className="font-semibold">Sirko Schindler</strong><br /><span className="text-text-2">German Aerospace Center (DLR), Institute of Data Science, Jena</span></li>
              <li><strong className="font-semibold">Daniel Garijo</strong><br /><span className="text-text-2">Ontology Engineering Group, Universidad Politécnica de Madrid</span></li>
              <li><strong className="font-semibold">Cristian Berrío</strong><br /><span className="text-text-2">Language Technology Research Lab, Expert.ai, Madrid</span></li>
              <li><strong className="font-semibold">Esteban González</strong><br /><span className="text-text-2">Ontology Engineering Group, Universidad Politécnica de Madrid</span></li>
            </ul>
          </div>
        </div>
        <div className="flex flex-wrap justify-between gap-3 border-t border-border pt-5 text-xs leading-[17px] text-muted-foreground">
          <span>
            Diagrams use the layout of the I-ADOPT Visualizer by Sirko Schindler,{' '}
            <a href="https://doi.org/10.5281/zenodo.18097903" target="_blank" rel="noopener noreferrer">doi:10.5281/zenodo.18097903</a>.
          </span>
          <span>Decompositions and vocabulary links are model-generated drafts.</span>
        </div>
      </div>
    </footer>
  );
}
