# ni-consolidation

## Add Module

You must install two modules to run this program.

- ni_mon_client : <https://github.com/dpnm-ni/ni-mano/tree/master/ni_mon_client_api/ni_mon_client>
- ni_nfvo_client : <https://github.com/dpnm-ni/ni-mano/tree/master/ni_nfvo_client_api/ni_nfvo_client>


## Run API Server (Flask)

```bash
$ python -m server
```

## Show GUI (Swagger UI)

- http://localhost:8012/ui/
- http://\<your-private-ip\>:8012/ui/

## 주의사항

해당 구현은 다음과 같은 환경을 가정으로 한 환경에서 KPI 성능인 25% 전원 절감을 달성할 수 있다.

1. NI Consolidation을 수행하기 이전에 모든 서버의 전원은 켜져있다.
2. Edge의 최대 load가 30% 이하를 가정으로 한다.
3. Edge의 종류와 VNF의 갯수에 따라서 서로 다른 모델을 사용한다.
4. Consolidation이 진행되는 와중에 VNF의 추가로 인한 에러는 고려하지 않는다.
5. 해당 구현은 항상 25%의 절감이 아닌 평균적으로 25%의 절감을 수행한다.
  - 100 번의 simulation 결과에서 대게의 경우 25% 이상의 서버를 Sleep 시킬 수 있었다.
  - 즉, 운이 좋지 않은 경우에는 하나의 서버도 끄지 못할 수도 있다.
