import { Router, type IRouter } from "express";
import healthRouter from "./health";
import emailsRouter from "./emails";
import jobsRouter from "./jobs";
import proxyRouter from "./proxy";
import deviceCheckRouter from "./deviceCheck";
import manualBrowserRouter from "./manualBrowser";

const router: IRouter = Router();

router.use(healthRouter);
router.use(emailsRouter);
router.use(jobsRouter);
router.use(proxyRouter);
router.use(deviceCheckRouter);
router.use(manualBrowserRouter);

export default router;
