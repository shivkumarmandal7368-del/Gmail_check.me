import { Router, type IRouter } from "express";
import healthRouter from "./health";
import emailsRouter from "./emails";
import jobsRouter from "./jobs";

const router: IRouter = Router();

router.use(healthRouter);
router.use(emailsRouter);
router.use(jobsRouter);

export default router;
