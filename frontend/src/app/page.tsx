// `/` é o painel: é a tela que se abre todo dia. O diagnóstico do setup, que morava
// aqui desde a Fase 0, foi para `/diagnostico`.

import { redirect } from "next/navigation";

export default function Home() {
  redirect("/painel");
}
